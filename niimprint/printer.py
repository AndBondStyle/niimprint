import abc
import asyncio
import enum
import logging
import math
import platform
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

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


def _count_bits(value: int) -> int:
    return value.bit_count()


def _count_pixels_for_bitmap_packet(data: bytes, printhead_pixels: int) -> tuple[int, int, int]:
    chunk_size = max(1, printhead_pixels // 8 // 3)
    parts = [0, 0, 0]
    for byte_n, value in enumerate(data):
        chunk_idx = min(2, byte_n // chunk_size)
        parts[chunk_idx] += _count_bits(value)
    return tuple(parts)


def _index_pixels(data: bytes) -> bytes:
    indexes = bytearray()
    for byte_pos, value in enumerate(data):
        for bit_pos in range(8):
            if value & (1 << (7 - bit_pos)):
                indexes.extend((byte_pos * 8 + bit_pos).to_bytes(2, "big"))
    return bytes(indexes)


class BaseTransport(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def read(self, length: int) -> bytes:
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, data: bytes):
        raise NotImplementedError

    def close(self):
        return None


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

    def close(self):
        self._sock.close()

    def __repr__(self):
        return "BluetoothTransport(rfcomm)"


class BleTransport(BaseTransport):
    SERVICE_UUID = "e7810a71-73ae-499d-8c15-faa9aef0c3f2"
    SCAN_TIMEOUT = 5.0
    READ_TIMEOUT = 0.5
    WRITE_INTERVAL = 0.01
    NAME_PREFIXES = ("B", "D")

    def __init__(self, address: str | None = None):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError(
                "BLE transport requires the optional 'bleak' dependency. "
                "Install it with `poetry install --extras ble` or `pip install bleak`."
            ) from exc

        self._BleakClient = BleakClient
        self._BleakScanner = BleakScanner
        self._address = address
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._buffer_ready = threading.Condition(self._buffer_lock)
        self._client = None
        self._characteristic = None
        self._closed = False
        try:
            self._call(self._connect())
        except Exception:
            self.close()
            raise

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def _normalize(self, value: str) -> str:
        return value.strip().lower()

    def _looks_like_mac(self, value: str) -> bool:
        return bool(value) and bool(
            re.fullmatch(r"([0-9a-fA-F]{2}:){5}([0-9a-fA-F]{2})", value)
        )

    def _is_candidate_device(self, device, adv) -> bool:
        uuids = {uuid.lower() for uuid in (adv.service_uuids or [])}
        if self.SERVICE_UUID in uuids:
            return True
        name = (device.name or adv.local_name or "").upper()
        return any(name.startswith(prefix) for prefix in self.NAME_PREFIXES)

    async def _discover_device(self):
        devices = await self._BleakScanner.discover(
            timeout=self.SCAN_TIMEOUT,
            return_adv=True,
        )
        target = self._normalize(self._address) if self._address is not None else None
        matches = []
        for device, adv in devices.values():
            if target is not None:
                if self._normalize(device.address) == target:
                    return device
                name = device.name or adv.local_name
                if name is not None and self._normalize(name) == target:
                    return device
            elif self._is_candidate_device(device, adv):
                matches.append(device)

        if target is not None:
            hint = ""
            if platform.system() == "Darwin" and self._looks_like_mac(self._address):
                hint = " On macOS, prefer the Bluetooth device name if the MAC address does not work."
            raise RuntimeError(
                f"No BLE Niimbot printer found matching {self._address!r}. "
                f"Make sure the printer is awake and advertising.{hint}"
            )
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            raise RuntimeError("No BLE Niimbot printers found")
        names = ", ".join(f"{dev.name or 'unknown'} ({dev.address})" for dev in matches)
        raise RuntimeError(f"Multiple BLE Niimbot printers found, please choose one with --addr: {names}")

    async def _connect(self):
        if platform.system() == "Linux" and self._address and shutil.which("bluetoothctl"):
            try:
                subprocess.run(
                    ["bluetoothctl", "remove", self._address],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception as exc:
                logging.debug(f"BLE cache clear skipped: {exc}")

        device = await self._discover_device()
        client = self._BleakClient(device)
        await client.connect()
        characteristic = None
        for service in client.services:
            if len(service.uuid) < 5:
                continue
            for candidate in service.characteristics:
                properties = set(candidate.properties)
                if "notify" in properties and "write-without-response" in properties:
                    characteristic = candidate
                    break
            if characteristic is not None:
                break

        if characteristic is None:
            await client.disconnect()
            raise RuntimeError("Suitable BLE characteristic not found")

        await client.start_notify(characteristic, self._handle_notification)
        self._client = client
        self._characteristic = characteristic

    def _handle_notification(self, _characteristic, data: bytearray):
        with self._buffer_ready:
            self._buffer.extend(data)
            self._buffer_ready.notify_all()

    def read(self, length: int) -> bytes:
        with self._buffer_ready:
            if not self._buffer and not self._closed:
                self._buffer_ready.wait(self.READ_TIMEOUT)
            if not self._buffer:
                return b""
            data = bytes(self._buffer[:length])
            del self._buffer[:length]
            return data

    def write(self, data: bytes):
        if self._closed:
            raise RuntimeError("BLE transport is closed")
        result = self._call(
            self._client.write_gatt_char(self._characteristic, data, response=False)
        )
        time.sleep(self.WRITE_INTERVAL)
        return result

    async def _disconnect(self):
        if self._client is not None:
            if self._characteristic is not None:
                try:
                    await self._client.stop_notify(self._characteristic)
                except Exception:
                    pass
            await self._client.disconnect()
        self._client = None
        self._characteristic = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._buffer_ready:
            self._buffer_ready.notify_all()
        try:
            self._call(self._disconnect())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1)
            self._loop.close()

    def __repr__(self):
        return f"BleTransport(address={self._address!r})"


class SerialTransport(BaseTransport):
    def __init__(self, port: str = "auto"):
        port = port if port != "auto" else self._detect_port()
        self._port = port
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

    def close(self):
        self._serial.close()

    def __repr__(self):
        return f"SerialTransport(port={self._port})"


class PrinterClient:
    STATUS_TIMEOUT = 10.0
    STATUS_POLL_INTERVAL = 0.3
    D110_M_DEVICE_TYPE = 2320

    def __init__(self, transport, model: str | None = None):
        self._transport = transport
        self._model = model
        self._device_type = None
        self._packetbuf = bytearray()

    def print_image(self, image: Image, density: int = 3):
        if self._uses_d110_m_v4_print_task():
            self._print_image_d110_m_v4(image, density)
            return

        self.set_label_density(density)
        self.set_label_type(1)
        self.start_print()
        if self._model in ("d11", "d110"):
            self.allow_print_clear()
        self.start_page_print()
        self.set_dimension(image.height, image.width)
        if self._model in ("d11", "d110"):
            self.set_quantity(1)
        for pkt in self._encode_image(image):
            self._send(pkt)
        self.end_page_print()
        if self._model in ("d11", "d110"):
            self._wait_for_print_status(page=1)
        time.sleep(0.3)
        while not self.end_print():
            time.sleep(0.1)

    def _print_image_d110_m_v4(self, image: Image, density: int):
        self.set_label_density(density)
        self.set_label_type(1)
        self.start_print_v4(total_pages=1)
        self.send_print_status_one_way()
        self.set_dimension_v4(image.height, image.width, quantity=1)
        for pkt in self._encode_image(image):
            self._send(pkt)
        self.end_page_print()
        self._wait_for_print_status(page=1)
        time.sleep(0.3)
        while not self.end_print():
            time.sleep(0.1)
        self.send_heartbeat_one_way()

    def _device_type_id(self):
        if self._device_type is None:
            self._device_type = self.get_info(InfoEnum.DEVICETYPE)
        return self._device_type

    def _uses_d110_m_v4_print_task(self) -> bool:
        return self._device_type_id() == self.D110_M_DEVICE_TYPE

    def _wait_for_print_status(self, page: int):
        deadline = time.monotonic() + self.STATUS_TIMEOUT
        while time.monotonic() < deadline:
            status = self.get_print_status()
            logging.debug(f"print status: {status}")
            if (
                status["page"] >= page
                and status["progress1"] >= 100
                and status["progress2"] >= 100
            ):
                return
            time.sleep(self.STATUS_POLL_INTERVAL)
        raise RuntimeError(f"Timed out waiting for print completion over {self._transport!r}")

    def _encode_image(self, image: Image):
        img = ImageOps.invert(image.convert("L")).convert("1")
        for y in range(img.height):
            row_pixels = [img.getpixel((x, y)) for x in range(img.width)]
            row_bits = "".join("0" if pix == 0 else "1" for pix in row_pixels)
            row_data = int(row_bits, 2).to_bytes(math.ceil(img.width / 8), "big")
            black_pixels = sum(bit == "1" for bit in row_bits)

            if black_pixels == 0:
                pkt = NiimbotPacket(0x84, struct.pack(">HB", y, 1))
                yield pkt
                continue

            counts = _count_pixels_for_bitmap_packet(row_data, image.width)
            header = struct.pack(">H3BB", y, *counts, 1)

            if black_pixels <= 6:
                pkt = NiimbotPacket(0x83, header + _index_pixels(row_data))
            else:
                pkt = NiimbotPacket(0x85, header + row_data)
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

    def _send_one_way(self, type_, data):
        packet = NiimbotPacket(type_, data)
        self._log_buffer("send", packet.to_bytes())
        self._send(packet)

    def _log_buffer(self, prefix: str, buff: bytes):
        msg = ":".join(f"{i:#04x}"[-2:] for i in buff)
        logging.debug(f"{prefix}: {msg}")

    def _response_timeout(self, reqcode):
        msg = (
            f"Printer did not respond to request {RequestCodeEnum(reqcode).name} "
            f"over {self._transport!r}"
        )
        if platform.system() == "Darwin" and isinstance(self._transport, SerialTransport):
            msg += (
                ". On macOS, paired Bluetooth devices may expose a serial port that opens "
                "successfully but does not actually speak the Niimbot protocol. Reconnect the "
                "printer first; if it still times out, this Bluetooth serial mapping is likely "
                "not usable and USB or Linux RFCOMM is required."
            )
        raise RuntimeError(msg)

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
        self._response_timeout(reqcode)

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

    def start_print_v4(self, total_pages: int = 1, color: int = 0, speed: int = 1):
        payload = struct.pack(">H4xBBB", total_pages, color, speed, 0)
        packet = self._transceive(RequestCodeEnum.START_PRINT, payload)
        return bool(packet.data[0])

    def end_print(self):
        packet = self._transceive(RequestCodeEnum.END_PRINT, b"\x01")
        return bool(packet.data[0])

    def send_heartbeat_one_way(self):
        self._send_one_way(RequestCodeEnum.HEARTBEAT, b"\x01")

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

    def set_dimension_v4(self, w, h, quantity: int = 1):
        payload = struct.pack(">HHHHBBBH", w, h, quantity, 0, 0, 0, 0, 0)
        packet = self._transceive(RequestCodeEnum.SET_DIMENSION, payload)
        return bool(packet.data[0])

    def set_quantity(self, n):
        packet = self._transceive(RequestCodeEnum.SET_QUANTITY, struct.pack(">H", n))
        return bool(packet.data[0])

    def send_print_status_one_way(self):
        self._send_one_way(RequestCodeEnum.GET_PRINT_STATUS, b"\x01")

    def get_print_status(self):
        packet = self._transceive(RequestCodeEnum.GET_PRINT_STATUS, b"\x01", 16)
        page, progress1, progress2 = struct.unpack(">HBB", packet.data[:4])
        return {"page": page, "progress1": progress1, "progress2": progress2}
