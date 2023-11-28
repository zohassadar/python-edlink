"""
Thanks to Krikzz
https://github.com/krikzz/EDN8-PRO
"""

from __future__ import annotations
import argparse
import base64
import binascii
import enum
import hashlib
import io
import logging
import os
import pathlib
import sys
import zlib

from serial import Serial

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEST_ROM_LEN = 40976

STATE_LENGTH = 0x100

BLOCK_SIZE = 8192
ACK_BLOCK_SIZE = 1024

FAT_READ = 0x01

CMD_STATUS = 0x10
CMD_MEM_RD = 0x19
CMD_MEM_WR = 0x1A
CMD_FPG_USB = 0x1E
CMD_FPG_SDC = 0x1F
CMD_F_FOPN = 0xC9
CMD_F_FRD = 0xCA
CMD_F_FCLOSE = 0xCE
CMD_F_FINFO = 0xD0

ADDR_SSR = 0x1802000
ADDR_FIFO = 0x1810000

BAUD_RATE = 115200
IDENTIFIER = "EverDrive N8"

CMD_TEST = ord("t")
CMD_REBOOT = ord("r")
CMD_HALT = ord("h")
CMD_SEL_GAME = ord("n")
CMD_RUN_GAME = ord("s")


if os.name == "posix":
    from serial.tools.list_ports_posix import comports
elif os.name == "nt":
    from serial.tools.list_ports_windows import comports
else:
    raise ImportError("Unsupported os: {os.name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("rom", nargs="?", help="Load this rom")
    parser.add_argument("-p", "--patch", help="Apply .ips patch to rom first")
    parser.add_argument("-t", "--test", action="store_true", help="Launch test rom")
    parser.add_argument("-s", "--sha1sum", help="sha1sum to validate patch", default="")
    parser.add_argument("-S", "--print-state", action="store_true")
    args = parser.parse_args()
    everdrive = Everdrive()
    if args.patch:
        rom = open_rom_with_patch(args.rom, args.patch, args.sha1sum)
        rom = NesRom(rom=rom, name=args.patch.replace(".ips", ".nes"))
        everdrive.load_game(rom)
    elif args.rom:
        rom = NesRom.from_file(args.rom)
        everdrive.load_game(rom)
    elif args.test:
        print("Launching fifo test rom")
        rom = get_test_rom()
        rom = NesRom(rom=rom, name="fifo_testrom.nes")
        everdrive.load_game(rom)
    elif args.print_state:
        everdrive.print_state()


def to_string(data: bytearray):
    return "-".join(f"{e:02x}".upper() for e in data)


class FileInfo:
    name: str
    size: int
    date: int
    time: int
    attrib: int


class Everdrive:
    def __init__(self):
        logger.debug(f"Initializing {type(self).__name__}()")
        self.set_serial_port()

    def set_serial_port(self):
        for port in comports():
            logger.debug(f"Found {port.device}: {port.description}")
            if port.description == IDENTIFIER:
                logger.info(f"Everdrive found on {port.device}")
                self.port = Serial(port=port.device, baudrate=BAUD_RATE, timeout=0.5)
                return
        raise RuntimeError(f"Unable to locate everdrive")

    def transmit_data(
        self,
        data: bytes,
        offset: int = 0,
        length: int = 0,
    ):
        if not length:
            length = len(data)
        logger.debug(f"Transmitting {len(data)}: {data[:48].hex()}")
        while length > 0:
            block = BLOCK_SIZE
            if block > length:
                block = length
            chunk = data[offset : offset + block]
            self.port.write(chunk)
            length -= block
            offset += block

    def receive_data(self, length: int) -> bytearray:
        data = self.port.read(length)
        data = bytearray(data)
        logger.debug(f"Received {len(data)}: {data[:48].hex()}")
        return data

    def transmit_command(self, command: int):
        cmd = bytearray(4)
        cmd[0] = ord("+")
        cmd[1] = ord("+") ^ 0xFF
        cmd[2] = command
        cmd[3] = command ^ 0xFF
        self.transmit_data(cmd)
        logger.debug(f"Transmitting command: {cmd.hex()}")

    def memory_read(self, address: int, length: int):
        logger.debug(f"Reading {length} from 0x{address:08x}")
        self.transmit_command(CMD_MEM_RD)
        self.transmit_32(address)
        self.transmit_32(length)
        self.transmit_8(0)
        return self.receive_data(length)

    def memory_write(self, address: int, data: bytearray):
        length = len(data)
        logger.debug(f"Writing {length} to {address:08x}")
        self.transmit_command(CMD_MEM_WR)
        self.transmit_32(address)
        self.transmit_32(length)
        self.transmit_8(0)
        self.transmit_data(data)

    def receive_8(self):
        result = self.receive_data(1).pop()
        logger.debug(f"receive_8: {result:02x}")
        return result

    def receive_16(self):
        result = self.receive_data(2)
        result = result[1] << 8 | result[0]
        logger.debug(f"receive_16: {result:04x}")
        return result

    def receive_32(self):
        result = self.receive_data(4)
        result = result[0] | (result[1] << 8) | (result[2] << 16) | (result[3] << 24)
        logger.debug(f"receive_16: {result:08x}")
        return result

    def transmit_32(self, data: int):
        logger.debug(f"transmit_32: {data:08x}")
        self.transmit_data(bytearray(data.to_bytes(length=4, byteorder="little")))

    def transmit_16(self, data: int):
        logger.debug(f"transmit_16: {data:04x}")
        self.transmit_data(bytearray(data.to_bytes(length=2, byteorder="little")))

    def transmit_8(self, data: int):
        logger.debug(f"transmit_8 {data:02x}")
        self.transmit_data(bytearray([data & 0xFF]))

    def print_state(self):
        state = self.memory_read(
            address=ADDR_SSR,
            length=STATE_LENGTH,
        )
        for i in range(0, STATE_LENGTH, 16):
            print(f"{to_string(state[i:i+8])}   {to_string(state[i+8:i+16])}")

    def write_fifo(self, data: bytearray):
        self.memory_write(ADDR_FIFO, data)

    def transmit_string_fifo(self, message: str):
        length = bytearray(len(message).to_bytes(2, "little"))
        data = bytearray(message.encode())
        logger.debug(f"string fifo: {length.hex()} {data.hex()}")
        self.write_fifo(length)
        self.write_fifo(data)

    def transmit_string(self, message: str):
        length = bytearray(len(message).to_bytes(2, "little"))
        data = bytearray(message.encode())
        logger.debug(f"string: {length.hex()} {data.hex()}")
        self.transmit_data(length)
        self.transmit_data(data)

    def command(self, command):
        data = bytearray(2)
        data[0] = ord("*")
        data[1] = command
        logger.debug(f"command: {data.hex()}")
        self.write_fifo(data)

    def load_game(self, rom: NesRom):
        logger.debug(f"Sending command to select game")
        self.command(CMD_SEL_GAME)

        rom_name = f"USB:{rom.name}"
        self.transmit_string_fifo(rom_name)
        logger.debug(f"Received: {self.receive_8()}")

        logger.debug(f"writing rom id to fifo")
        rom_id = rom.get_rom_id()

        logger.debug(f"rom id: {'-'.join(f'{b:02x}'.upper() for b in rom_id)}")
        self.write_fifo(rom_id)
        logger.debug(f"Received: {self.receive_8()}")

        logger.debug(f"getting 2 bytes for map_idx")
        map_idx = self.receive_16()

        logger.debug(f"Running the game:  {map_idx}")
        self.command(CMD_RUN_GAME)
        logger.debug(f"Received: {self.receive_8()}")
        self.memory_write(rom.ADDR_PRG, rom.prg)
        self.memory_write(rom.ADDR_CHR, rom.chr)
        self.map_load_sdc(map_idx)

    def fpg_init_direct(self):
        """
        Unused.  Setup as a test while troubleshooting.
        """
        fpg = bytearray(open("004.RBF", "rb").read())
        self.transmit_command(CMD_FPG_USB)
        self.transmit_32(len(fpg))
        self.transmit_data_ack(fpg)
        self.check_status()

    def file_info(self, path: str):
        logger.debug(f"Requesting file info")
        self.transmit_command(CMD_F_FINFO)
        self.transmit_string(path)
        response = self.receive_8()
        if response:
            raise RuntimeError(f"File access error: {response:02x}")
        return self.receive_file_info()

    def receive_file_info(self) -> FileInfo:
        logger.debug(f"Receiving file info")
        fileinfo = FileInfo()
        fileinfo.size = self.receive_32()
        fileinfo.date = self.receive_16()
        fileinfo.time = self.receive_16()
        fileinfo.attrib = self.receive_8()
        fileinfo.name = self.receive_string()
        return fileinfo

    def receive_string(self):
        length = self.receive_16()
        logger.debug(f"Receiving String of {length} length")
        data = self.receive_data(length)
        return bytes(data).decode()

    def fpg_init(self, path: str):
        logger.debug(f"Initializing FPG: {path}")
        fileinfo = self.file_info(path)
        self.open_file(path, FAT_READ)
        self.transmit_command(CMD_FPG_SDC)
        self.transmit_32(fileinfo.size)
        self.transmit_8(0)
        self.check_status()

    def check_status(self):
        logger.debug(f"Checking Status")
        response = self.get_status()
        if response:
            raise RuntimeError(f"Operation error: {response:02x}")

    def get_status(self):
        self.transmit_command(CMD_STATUS)
        response = self.receive_16()
        if (response & 0xFF00) != 0xA500:
            raise RuntimeError(f"Unexpected response: {response:04x}")
        return response & 0xFF

    def transmit_data_ack(self, data):
        """
        unused.  set up along fpg_init_direct
        """
        length = len(data)
        offset = 0
        while length > 0:
            response = self.receive_8()
            if response:
                raise RuntimeError(f"Tx ack: {response:02x}")
            block = ACK_BLOCK_SIZE
            if block > length:
                block = length
            self.transmit_data(data[offset : offset + block])
            length -= block
            offset += block

    def map_load_sdc(self, map_id: int):
        map_path = "EDN8/MAPS/"
        self.open_file("EDN8/MAPROUT.BIN", FAT_READ)
        map_data = self.read_file(4096)
        self.close_file()

        map_pkg = map_data[map_id]
        if map_pkg < 100:
            map_path += "0"
        if map_pkg < 10:
            map_path += "0"
        map_path = f"{map_path}{map_pkg}.RBF"
        logger.debug(f"int mapper: {map_path}")
        self.fpg_init(map_path)

    def open_file(self, path: str, mode: int):
        logger.debug(f"Opening: {path}")
        self.transmit_command(CMD_F_FOPN)
        logger.debug(f"File open command: {CMD_F_FOPN}")
        self.transmit_8(mode)
        logger.debug(f"File open mode: {mode}")
        self.transmit_string(path)
        logger.debug(f"File open path: {path}")
        self.check_status()

    def close_file(self):
        logger.debug(f"CLOSING FILE")
        self.transmit_command(CMD_F_FCLOSE)
        self.check_status()

    def read_file(self, length: int) -> bytearray:
        logger.debug("Receiving data from file")
        self.transmit_command(CMD_F_FRD)
        self.transmit_32(length)
        data = bytearray(length)
        offset = 0
        while length > 0:
            block = 4096
            if block > length:
                block = length
            response = self.receive_8()
            if response:
                raise Exception(f"File read error: {response:02x}")
            data[offset : offset + block] = self.receive_data(block)
            offset += block
            length -= block
        return data


class NesRom:
    ROM_TYPE_NES = 0
    MIR_HOR = "H"
    MIR_VER = "V"
    MIR_4SC = "4"
    MIR_1SC = "1"
    MAX_ID_CALC_LEN = 0x100000
    ADDR_PRG = 0x0000000
    ADDR_CHR = 0x0800000

    def __init__(self, rom: bytearray, name: str):
        self.rom_type = self.ROM_TYPE_NES
        self.name = name
        self.rom = rom
        self.size = len(self.rom)
        self.ines = self.rom[:32]
        self.nes = self.ines[0:3] == bytearray(b"NES")
        if not self.nes:
            raise RuntimeError(f"This script only supports nes: {self.ines[0:3]}")
        self.dat_base = 16
        self.prg_size = self.rom[4] * 1024 * 16
        self.chr_size = self.rom[5] * 1024 * 8
        self.srm_size = 8192
        if not self.prg_size:
            self.prg_size = 0x400000
        self.mapper = (self.rom[6] >> 4) | (self.rom[7] & 0xF0)
        self.mirroring = self.MIR_VER if self.rom[6] & 1 else self.MIR_HOR
        self.bat_ram = bool((self.rom[6] & 2))
        if self.rom[6] & 8:
            self.mirroring = self.MIR_4SC
        self.crc = zlib.crc32(self.rom[self.dat_base :])
        if self.mapper == 255:
            raise RuntimeError(f"OS mapper not yet supported")
        self.prg = self.rom[16 : 16 + self.prg_size]
        self.chr = self.rom[16 + self.prg_size : 16 + self.prg_size + self.chr_size]

        logger.debug(f"{self.mapper=}")
        logger.debug(f"{self.prg_size=}")
        logger.debug(f"{self.chr_size=}")
        logger.debug(f"{self.srm_size=}")
        logger.debug(f"{self.mirroring=}")
        logger.debug(f"{self.bat_ram=}")
        logger.debug(f"{self.crc=:08x}")

    @classmethod
    def from_file(cls, file):
        name = pathlib.Path(file).name
        rom = bytearray(bytes(open(file, "rb").read()))
        return cls(rom=rom, name=name)

    def get_rom_id(self):
        logger.debug("Getting rom ID")
        offset = len(self.ines)
        data = bytearray(offset + 12)
        data[:offset] = self.ines
        data[offset : offset + 4] = bytearray(self.size.to_bytes(4, "little"))
        data[offset + 4 : offset + 8] = bytearray(self.crc.to_bytes(4, "little"))
        data[offset + 8 :] = bytearray((16).to_bytes(4, "little"))
        return data


def apply_ips_patch(rom: bytearray, patch: bytes, sha1sum: str = "") -> bytearray:
    ptr = 5
    while ptr < len(patch):
        offset = int.from_bytes(patch[ptr : ptr + 3], "big")
        size = int.from_bytes(patch[ptr + 3 : ptr + 5], "big")
        ptr += 5
        if size:
            rom[offset : offset + size] = patch[ptr : ptr + size]
            ptr += size
        else:
            data = patch[ptr + 2 : ptr + 3] * int.from_bytes(
                patch[ptr : ptr + 2], "big"
            )
            rom[offset : offset + len(data)] = data
            ptr += 3
        if patch[ptr:] == b"EOF":
            ptr += 3
    if sha1sum and (new_sha1sum := hashlib.sha1(rom).digest().hex()) != sha1sum.lower():
        print(f"Invalid sha1sum: {new_sha1sum}", file=sys.stderr)
        sys.exit(1)
    elif sha1sum:
        print("Valid sha1sum")
    return rom


def get_test_rom() -> bytearray:
    rom = bytearray(TEST_ROM_LEN)
    patch = base64.b85decode(fifo_testrom)
    bps_patch = BPSPatch(patch)
    return bps_patch.patch_rom(rom)


def open_rom_with_patch(rom_file: str, patch_file: str, sha1sum: str = "") -> bytearray:
    patch = open(patch_file, "rb").read()
    if patch[:5] != b"PATCH":
        print(f"{patch_file} doesn't look like a patch", file=sys.stderr)
        sys.exit(1)
    rom = bytearray(open(rom_file, "rb").read())
    return apply_ips_patch(rom, patch, sha1sum)


def count_bytes(everdrive: Everdrive):
    counter = 0
    while True:
        byte_ = everdrive.receive_data(1)
        if not byte_:
            break
        counter += 1
    print(counter)


def send_fifo(everdrive: Everdrive, *numbers):
    payload = bytearray(n & 0xFF for n in numbers)
    everdrive.write_fifo(payload)


TEST_ROM_LEN = 40976

fifo_testrom = (
    b"LQqpN5I=zsKY@UmPDN810s#Y}fYqf9&;+Fp@H6HYsVj}}KuQ0N@Ia{|pSXpk4Udk24v&fD45SXG4e$ny"
    b"@IcDY`iQ{)(Dh*E7p?I?g$j?jeSz>mg$rQj7pVY+6@?b9@j%Hz&<w5cK;{+D^5zyyRDfWq0gV<wg#}8D"
    b"7C@~QKuRVBkoctqNJR!E1%(A@g$7_qib$A9<^rh!g$Jo1jRqj8h>Zpyq7SM6jRzpg5c-!WfPtZpg2j@c"
    b"fgsR<r4Nk<Af*tE2Oy%Fm#TpgkAi^`kD7rKkAjsHjRzp9A&>u$3V?;8qnF=}3LxQum&lXtfw7l@feVlB"
    b"fg+=;fxnl@l@=i3fu$9r_>--H&6lpV7a-w*rU<=+fsF<ry@-#-p{ekV2Oy;h@EDU{fxV^)y^DbuX@?kP"
    b"35^CIsqv2lfRzA^1t5(Dj}L%pfB+z=4vhgIAZUPSsA#HaKp-I@BB3FnApigf2nYxuQ)O;sAS3`FV{dhC"
    b"bRZ-EAW~&vWFRCoFlI45uMC5MFpq+zY-MgJadl;NWhsw|DS;#ah5&>Cpa8J|@CJ<sAjr`6U`R?zN=l6f"
    b"AZRHMjRzoLr3lbPr3EPh@Ce2U5Co|Pg$bnvDFN^d<_V<<$p_E`sQ`rur3vsMrjH(g#R%{S_71fUDG!wn"
    b"r3EPj@CeqMxQR?zf|s40#t0AusRM-wlaQGV<_M(-$pn+Xu?VFR3JMAe1rmi0r4y47fRPl14W$p0iy49t"
    b"g$$(yDS+?=r3fIEf~f$72c-w_{7mqH0DwGzM1V|y3aY)0f`tIRj)H{&Yybdw*rGs=7eJ!_n&^%IAdUeb"
    b"ju1dB0w62`AQ1bCl>nUp0i6H>od5%!00f-?1)Tr}od5^u(B_jifR|f`fdG$!fdG?&iRjS!m-4#?hm%)^"
    b"yH<vQBcp<Wz?W%=jnMk3Kc`BFnY~wo$^Y;NkIjipJcE}5g{dBm0U%89fe$b+Fz-Nyf&T_$hLg33fTV(h"
    b"fgUqAKRYuX0G}Fhf|U#$3=9kmr;M2%GY1YFI6te7i31J;lgpRTnFbCPCO-xSqmGR~Fg^p1sh_Zs9y2gL"
    b"GpmZ3KO+MM1`G_Nj)@*KGmnt3i-8}P-=pA-KQl8kqllS)Gc!IjGk&9vnIAJTFfcJ5qmGNAi>{Nuldze8"
    b"F)%zZF@Lv?g)nfVgg=KJhnW}{7#J7;7^5nOiEM00lShZ2Ooxdme0-0Pql=jkJa9N1co3tGna0i>IB@31"
    b"qmG$4WN<ocW;&yei5M6Vn>&D;K!AZTkC2OBfS*u+k8g;945x62l>lrw{y1!pXMme>fRPv&ei#_5ijxU|"
    b"fe5b(fPp-lF^ZouijT9SvY8AF92ht-Fr$u{JZ5fwc4j=Ij+GcV7#J8Br;M3AW)2)UaDJ<fi3S`7lgpRT"
    b"nG75(OnwXuqmGS!V0;FTsh_ZsJZ4~gW~+*sentii3>X-rj)^>GW{;4si-A6u-<RNzS%I7Rt5AZ02&00T"
    b"3>YwAFc=J@j*oAGmxY&df{Uq*1`HT5qp+1cW(Eux0FPCGpJ0HEZf;;=lWBsRaDtI$er9H7ql%gQW@db5"
    b"X8fa$jXq{#V2{b4(2Jpqu9Lr$u$lg1V0d6+{<n^WVBn*KnS92;&c?=kqmGN~lh>a>fPp-dVTge|qhyGg"
    b"J`4;D3`{tqj+ypmY<O&D_M?uOcrY+9Ffx9lj+w^p{?^9E#-omrW@dJNZj&c~moR{VJd;m>lkt;4f{!7B"
    b"iC|#xqb7orp@nQZqokK%f`R6vVuG1IVqiQ5Mtq}=g??I>J%OJ<fs+q|fo8W5gNbfD7^8-n#>U3h{%k~}"
    b"j*(_&JQzHaXM&$_f{VR@7>|>e(3yTt95`@dexr_oJd;n0fjpy9i<vMmI2arZ45N;LJd<yWfjpyfi-8c6"
    b"S&M-Hqg;!ZX_kS0qiU9!FgP3x0001^j+p=eJO(~yKBJDAU|@V^W@db&j*Fp<VqjuCqo|n%20mtHW<H~i"
    b"i=l~TeqfV}ldzQz7#KVl7^jS#002H_W<CZypOBqkU~G0~W@Z4Nkck)oIG1aTn{bSU3;>gVn1MK-1euj!"
    b"U}S7~Y>&m4(34M%g%~`eQjM7a0PNP**4C?zg<NKj9e}4GfRA;Cmqdh@FoA)5lVgUbWQLP5fq@36GJ%O~"
    b"c3_X0qn?dCU_1tsX@Zk*f|)Qdd@wLD9HWkb0F!fufjYN#hJ`#BtAvq8R#sL%tBR9zhOcsliDo_qm;09h"
    b"fRTI{I52#pij5pFFkp{KfS*u+lV5_XY=WIQ3=9Sg3^)Lvf{_3KGJZ^x1(TN!lbt+5nW3ebLOh?41Gj&="
    b"nLuE0cyM4qqYS%^0DnFllXtJ5ey?|feZ7JIy9~bnufI=%fBplzM+U-2;zCt>PBw!y"
)

"""
BPS Code from https://github.com/mgius/python-bpspatcher

"""

ENDIAN = "little"


class Action(enum.IntEnum):
    SourceRead = 0
    TargetRead = 1
    SourceCopy = 2
    TargetCopy = 3


def convert_uint(b: bytes):
    return int.from_bytes(b, ENDIAN, signed=False)


def read_number_io(b: io.BytesIO) -> int:
    data, shift = 0, 1

    # this was basically directly copied from the bps_spec
    while True:
        x = b.read(1)
        if len(x) == 0:
            return None
        x = convert_uint(x)
        data += (x & 0x7F) * shift
        if x & 0x80:
            break
        shift <<= 7
        data += shift

    return data


def read_number(b: bytes) -> tuple:
    """Read a number that starts at the beginning of the bytes

    returns a tuple of the number read and remaining bytes
    """
    bio = io.BytesIO(b)
    data = read_number_io(bio)
    return data, bio.read()


class InvalidPatch(Exception):
    def __init__(self, msg):
        self.msg = msg


class BPSPatch(object):
    MAGIC_HEADER = "BPS1".encode("UTF-8")

    def __init__(self, patch: bytes):
        header = patch[:4]

        if header != self.MAGIC_HEADER:
            raise InvalidPatch(f"Magic header {header} is incorrect")

        self.source_checksum = convert_uint(patch[-4 * 3 : -4 * 2])
        self.target_checksum = convert_uint(patch[-4 * 2 : -4 * 1])
        self.patch_checksum = convert_uint(patch[-4 * 1 :])

        calculated_checksum = binascii.crc32(patch[:-4])

        if self.patch_checksum != calculated_checksum:
            raise InvalidPatch(
                f"Patch Checksum {self.patch_checksum} does not match "
                f"actual checksum {calculated_checksum}"
            )

        remainder = patch[4:]

        self.source_size, remainder = read_number(remainder)
        self.target_size, remainder = read_number(remainder)
        self.metadata_size, remainder = read_number(remainder)

        self.metadata = remainder[: self.metadata_size].decode("UTF-8")

        # actions is everything else other than the header and footer
        self.actions = remainder[self.metadata_size : -12]

    def patch_rom(self, source: bytes) -> bytes:
        if len(source) != self.source_size:
            raise InvalidPatch(
                f"source size {len(source)} does not match "
                f"expected {self.source_size}"
            )

        source_checksum = binascii.crc32(source)
        if source_checksum != self.source_checksum:
            raise InvalidPatch(
                f"source checksum {source_checksum} does not match "
                f"expected {self.source_checksum}"
            )

        target = bytearray(self.target_size)

        actions = io.BytesIO(self.actions)

        output_offset = 0
        source_relative_offset = 0
        target_relative_offset = 0

        while True:
            action = read_number_io(actions)
            if action is None:
                break

            command = action & 3
            length = (action >> 2) + 1

            # Modified from original
            logger.debug(f"BPS Command {command}, length {length}")

            if command == Action.SourceRead:
                # consume some number of bytes from source file
                target[output_offset : output_offset + length] = source[
                    output_offset : output_offset + length
                ]
                output_offset += length

            elif command == Action.TargetRead:
                # consume some number of bytes from patch file
                target[output_offset : output_offset + length] = actions.read(length)
                output_offset += length

            elif command == Action.SourceCopy:
                # consume some number of bytes from source file, but from
                # somewhere else.  This action seems unnecessarily complicated
                data = read_number_io(actions)
                source_relative_offset += (-1 if data & 1 else 1) * (data >> 1)
                target[output_offset : output_offset + length] = source[
                    source_relative_offset : source_relative_offset + length
                ]

                output_offset += length
                source_relative_offset += length

            elif command == Action.TargetCopy:
                # consume some number of bytes from the target file
                data = read_number_io(actions)
                target_relative_offset += (-1 if data & 1 else 1) * (data >> 1)
                # unfortunately it is not safe to optimize this, as one of the
                # documented use cases is to write a single byte then duplicate
                # that byte over and over filling out an array.
                for _ in range(length):
                    target[output_offset] = target[target_relative_offset]
                    output_offset += 1
                    target_relative_offset += 1

        target_checksum = binascii.crc32(target)

        if target_checksum != self.target_checksum:
            raise InvalidPatch(
                f"target checksum {target_checksum} does not match "
                f"expected {self.target_checksum}"
            )

        return target


"""
end BPS Code
"""

if __name__ == "__main__":
    main()
