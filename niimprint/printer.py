import abc
import asyncio
import enum
import logging
import math
import socket
import struct
import threading
import time
from queue import Empty, Queue

import serial
from PIL import Image, ImageOps
from serial.tools.list_ports import comports as list_comports

from niimprint.packet import NiimbotPacket


class InfoEnum(enum.IntEnum):
    DENSITY = 1
    PRINTSPEED = 2
    LABELTYPE = 3
    LANGUAGETYPE = 6
    AUTOSHUTDOWNTIME = 7
    DEVICETYPE = 8
    SOFTVERSION = 9
    BATTERY = 10
    DEVICESERIAL = 11
    HARDVERSION = 12


class RequestCodeEnum(enum.IntEnum):
    GET_INFO = 64  # 0x40
    GET_RFID = 26  # 0x1A
    HEARTBEAT = 220  # 0xDC
    SET_LABEL_TYPE = 35  # 0x23
    SET_LABEL_DENSITY = 33  # 0x21
    START_PRINT = 1  # 0x01
    END_PRINT = 243  # 0xF3
    START_PAGE_PRINT = 3  # 0x03
    END_PAGE_PRINT = 227  # 0xE3
    ALLOW_PRINT_CLEAR = 32  # 0x20
    SET_DIMENSION = 19  # 0x13
    SET_QUANTITY = 21  # 0x15
    GET_PRINT_STATUS = 163  # 0xA3


def _packet_to_int(x):
    return int.from_bytes(x.data, "big")


class BaseTransport(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def read(self, length: int) -> bytes:
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, data: bytes):
        raise NotImplementedError


class BluetoothTransport(BaseTransport):
    def __init__(self, address: str):
        self._sock = socket.socket(
            socket.AF_BLUETOOTH,
            socket.SOCK_STREAM,
            socket.BTPROTO_RFCOMM,
        )
        self._sock.connect((address, 1))

    def read(self, length: int) -> bytes:
        return self._sock.recv(length)

    def write(self, data: bytes):
        return self._sock.send(data)


# Niimbot BLE GATT — same on D11/D110/D101/B1/B18/B21, verified across forks
# (NiimBlue web tool, niimprint-android, etc). The protocol char is duplex
# (notify + write-without-response).
NIIMBOT_BLE_SERVICE = "e7810a71-73ae-499d-8c15-faa9aef0c3f2"
NIIMBOT_BLE_CHAR = "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"


class BleakTransport(BaseTransport):
    """Bluetooth Low Energy transport for Niimbot models that don't advertise
    classic RFCOMM (e.g. newer D110 batches, which only expose a BLE GATT
    transparent-serial bridge).

    bleak is async-only; this class runs an asyncio loop in a background
    thread so the surrounding sync PrinterClient code is unchanged. Inbound
    notifications go into a Queue that ``read()`` drains.
    """

    def __init__(self, address: str, timeout: float = 10.0):
        # Lazy import so users on RFCOMM-only setups don't need bleak installed.
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as e:
            raise RuntimeError(
                "BLE transport requires the 'bleak' package — install with "
                "`pip install bleak` (or `pip install niimprint[ble]`)."
            ) from e

        self._BleakClient = BleakClient
        self._BleakScanner = BleakScanner
        self._address = address
        self._timeout = timeout
        self._rx: Queue[bytes] = Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="niimprint-ble"
        )
        self._thread.start()
        self._run(self._connect(), timeout=timeout + 20)

    def _run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self):
        # On Linux/BlueZ, a device cached as "Connected: yes" from a prior
        # session (or a stale entry between runs) causes BleakClient.connect
        # to throw BleakDeviceNotFoundError. Pre-clear with `bluetoothctl
        # remove` (idempotent — silently no-ops if the device isn't in the
        # cache). On macOS/Windows this is a harmless no-op since bluetoothctl
        # isn't present.
        try:
            import shutil
            import subprocess

            if shutil.which("bluetoothctl"):
                subprocess.run(
                    ["bluetoothctl", "remove", self._address],
                    capture_output=True, timeout=5, check=False,
                )
        except Exception as e:
            logging.debug("BLE: pre-connect cache clear skipped (%s)", e)

        # Now scan: re-populates BlueZ's cache with a fresh entry so the
        # subsequent connect won't hit a stale-state BleakDeviceNotFoundError.
        # find_device_by_address returns None on timeout, in which case we
        # fall back to the bare BleakClient (covers backends that don't need
        # the pre-scan, e.g. macOS CoreBluetooth).
        logging.debug("BLE: scanning for %s ...", self._address)
        device = await self._BleakScanner.find_device_by_address(
            self._address, timeout=self._timeout
        )
        self._client = self._BleakClient(
            device or self._address, timeout=self._timeout
        )
        await self._client.connect()
        # Discover whether the protocol char lives where we expect; fall back
        # to scanning for any writable + notify char if the printer ever ships
        # with a different UUID layout.
        char = self._client.services.get_characteristic(NIIMBOT_BLE_CHAR)
        if char is None:
            for service in self._client.services:
                for c in service.characteristics:
                    props = set(c.properties)
                    if {"notify", "write-without-response"} <= props or (
                        "notify" in props and "write" in props
                    ):
                        char = c
                        break
                if char:
                    break
        if char is None:
            raise RuntimeError(
                "No writable+notify GATT characteristic found on device — "
                "unexpected BLE layout for a Niimbot printer."
            )
        self._char = char
        await self._client.start_notify(char, self._on_notify)

    def _on_notify(self, _sender, data: bytearray):
        self._rx.put(bytes(data))

    def read(self, length: int) -> bytes:
        # PrinterClient asks for up to 1024 bytes and tolerates short reads;
        # we block briefly for the first chunk then drain any others already
        # delivered so the recv loop sees complete packets when possible.
        try:
            buf = self._rx.get(timeout=1.0)
        except Empty:
            return b""
        while True:
            try:
                buf += self._rx.get_nowait()
            except Empty:
                break
        return buf

    def write(self, data: bytes):
        # response=False matches NiimBlue's writeValueWithoutResponse; bleak
        # on BlueZ chunks to the negotiated MTU automatically.
        self._run(
            self._client.write_gatt_char(self._char, bytes(data), response=False)
        )

    def close(self):
        try:
            self._run(self._client.disconnect(), timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


class SerialTransport(BaseTransport):
    def __init__(self, port: str = "auto"):
        port = port if port != "auto" else self._detect_port()
        self._serial = serial.Serial(port=port, baudrate=115200, timeout=0.5)

    def _detect_port(self):
        all_ports = list(list_comports())
        if len(all_ports) == 0:
            raise RuntimeError("No serial ports detected")
        if len(all_ports) > 1:
            msg = "Too many serial ports, please select specific one:"
            for port, desc, hwid in all_ports:
                msg += f"\n- {port} : {desc} [{hwid}]"
            raise RuntimeError(msg)
        return all_ports[0][0]

    def read(self, length: int) -> bytes:
        return self._serial.read(length)

    def write(self, data: bytes):
        return self._serial.write(data)


class PrinterClient:
    def __init__(self, transport):
        self._transport = transport
        self._packetbuf = bytearray()

    def print_image(self, image: Image, density: int = 3):
        self.set_label_density(density)
        self.set_label_type(1)
        self.start_print()
        # self.allow_print_clear()  # Something unsupported in protocol decoding (B21)
        self.start_page_print()
        self.set_dimension(image.height, image.width)
        # self.set_quantity(1)  # Same thing (B21)
        for pkt in self._encode_image(image):
            self._send(pkt)
        self.end_page_print()
        time.sleep(0.3)  # FIXME: Check get_print_status()
        while not self.end_print():
            time.sleep(0.1)

    def _encode_image(self, image: Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        for y in range(img.height):
            line_data = [img.getpixel((x, y)) for x in range(img.width)]
            line_data = "".join("0" if pix == 0 else "1" for pix in line_data)
            line_data = int(line_data, 2).to_bytes(math.ceil(img.width / 8), "big")
            counts = (0, 0, 0)  # It seems like you can always send zeros
            header = struct.pack(">H3BB", y, *counts, 1)
            pkt = NiimbotPacket(0x85, header + line_data)
            yield pkt

    def _recv(self):
        packets = []
        self._packetbuf.extend(self._transport.read(1024))
        while len(self._packetbuf) > 4:
            pkt_len = self._packetbuf[3] + 7
            if len(self._packetbuf) >= pkt_len:
                packet = NiimbotPacket.from_bytes(self._packetbuf[:pkt_len])
                self._log_buffer("recv", packet.to_bytes())
                packets.append(packet)
                del self._packetbuf[:pkt_len]
        return packets

    def _send(self, packet):
        self._transport.write(packet.to_bytes())

    def _log_buffer(self, prefix: str, buff: bytes):
        msg = ":".join(f"{i:#04x}"[-2:] for i in buff)
        logging.debug(f"{prefix}: {msg}")

    def _transceive(self, reqcode, data, respoffset=1):
        respcode = respoffset + reqcode
        packet = NiimbotPacket(reqcode, data)
        self._log_buffer("send", packet.to_bytes())
        self._send(packet)
        resp = None
        for _ in range(6):
            for packet in self._recv():
                if packet.type == 219:
                    raise ValueError
                elif packet.type == 0:
                    raise NotImplementedError
                elif packet.type == respcode:
                    resp = packet
            if resp:
                return resp
            time.sleep(0.1)
        return resp

    def get_info(self, key):
        if packet := self._transceive(RequestCodeEnum.GET_INFO, bytes((key,)), key):
            match key:
                case InfoEnum.DEVICESERIAL:
                    return packet.data.hex()
                case InfoEnum.SOFTVERSION:
                    return _packet_to_int(packet) / 100
                case InfoEnum.HARDVERSION:
                    return _packet_to_int(packet) / 100
                case _:
                    return _packet_to_int(packet)
        else:
            return None

    def get_rfid(self):
        packet = self._transceive(RequestCodeEnum.GET_RFID, b"\x01")
        data = packet.data

        if data[0] == 0:
            return None
        uuid = data[0:8].hex()
        idx = 8

        barcode_len = data[idx]
        idx += 1
        barcode = data[idx : idx + barcode_len].decode()

        idx += barcode_len
        serial_len = data[idx]
        idx += 1
        serial = data[idx : idx + serial_len].decode()

        idx += serial_len
        total_len, used_len, type_ = struct.unpack(">HHB", data[idx:])
        return {
            "uuid": uuid,
            "barcode": barcode,
            "serial": serial,
            "used_len": used_len,
            "total_len": total_len,
            "type": type_,
        }

    def heartbeat(self):
        packet = self._transceive(RequestCodeEnum.HEARTBEAT, b"\x01")
        closingstate = None
        powerlevel = None
        paperstate = None
        rfidreadstate = None

        match len(packet.data):
            case 20:
                paperstate = packet.data[18]
                rfidreadstate = packet.data[19]
            case 13:
                closingstate = packet.data[9]
                powerlevel = packet.data[10]
                paperstate = packet.data[11]
                rfidreadstate = packet.data[12]
            case 19:
                closingstate = packet.data[15]
                powerlevel = packet.data[16]
                paperstate = packet.data[17]
                rfidreadstate = packet.data[18]
            case 10:
                closingstate = packet.data[8]
                powerlevel = packet.data[9]
                rfidreadstate = packet.data[8]
            case 9:
                closingstate = packet.data[8]

        return {
            "closingstate": closingstate,
            "powerlevel": powerlevel,
            "paperstate": paperstate,
            "rfidreadstate": rfidreadstate,
        }

    def set_label_type(self, n):
        assert 1 <= n <= 3
        packet = self._transceive(RequestCodeEnum.SET_LABEL_TYPE, bytes((n,)), 16)
        return bool(packet.data[0])

    def set_label_density(self, n):
        assert 1 <= n <= 5  # B21 has 5 levels, not sure for D11
        packet = self._transceive(RequestCodeEnum.SET_LABEL_DENSITY, bytes((n,)), 16)
        return bool(packet.data[0])

    def start_print(self):
        packet = self._transceive(RequestCodeEnum.START_PRINT, b"\x01")
        return bool(packet.data[0])

    def end_print(self):
        packet = self._transceive(RequestCodeEnum.END_PRINT, b"\x01")
        return bool(packet.data[0])

    def start_page_print(self):
        packet = self._transceive(RequestCodeEnum.START_PAGE_PRINT, b"\x01")
        return bool(packet.data[0])

    def end_page_print(self):
        packet = self._transceive(RequestCodeEnum.END_PAGE_PRINT, b"\x01")
        return bool(packet.data[0])

    def allow_print_clear(self):
        packet = self._transceive(RequestCodeEnum.ALLOW_PRINT_CLEAR, b"\x01", 16)
        return bool(packet.data[0])

    def set_dimension(self, w, h):
        packet = self._transceive(
            RequestCodeEnum.SET_DIMENSION, struct.pack(">HH", w, h)
        )
        return bool(packet.data[0])

    def set_quantity(self, n):
        packet = self._transceive(RequestCodeEnum.SET_QUANTITY, struct.pack(">H", n))
        return bool(packet.data[0])

    def get_print_status(self):
        packet = self._transceive(RequestCodeEnum.GET_PRINT_STATUS, b"\x01", 16)
        page, progress1, progress2 = struct.unpack(">HBB", packet.data)
        return {"page": page, "progress1": progress1, "progress2": progress2}
