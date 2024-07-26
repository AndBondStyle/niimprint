import abc
import enum
import logging
import math
import socket
import struct
import time

import serial
from PIL import Image, ImageOps
from serial.tools.list_ports import comports, grep as comports_grep

from packet import NiimbotPacket
from printer_models import supported_models


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
    UNKNOWN_1 = 13
    UNKNOWN_2 = 15


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


class SerialTransport(BaseTransport):
    def __init__(self, port: str = "auto", model: str = "auto", verbose: bool = False):
        self._model = model
        self._port = port
        self._serial_number = None
        self._verbose = verbose
        self._detect_port_and_model()
        print(f"Connecting to printer {self._model.upper()} (Serial No. {self._serial_number}) on {self._port}")

        self._serial = serial.Serial(port=self._port, baudrate=115200, timeout=0.5)

    def _detect_port_and_model(self):
        com_ports = comports()
        if self._port != "auto" and self._model != "auto":
            return
        elif self._port == "auto":
            com_ports = comports()
        else:
            com_ports = list(comports_grep(port))

        if len(com_ports) == 0:
            raise RuntimeError("No serial ports detected")

        # If at least one COM port was detected, filter the USB one and further filter by model name using the serial number
        if len(com_ports) > 0:
            detected_devices = []
            for i in range(len(com_ports)):
                if "USB" in com_ports[i].hwid:
                    if self._verbose:
                        print(f"{com_ports[i].device}:\n",
                            f"\tName:         {com_ports[i].name}\n",
                            f"\tDescription:  {com_ports[i].description}\n",
                            f"\tHWID:         {com_ports[i].hwid}\n",
                            f"\tVID:          {com_ports[i].vid}\n",
                            f"\tPID:          {com_ports[i].pid}\n",
                            f"\tSerial No.:   {com_ports[i].serial_number}\n",
                            f"\tLocation:     {com_ports[i].location}\n",
                            f"\tManufacturer: {com_ports[i].manufacturer}\n",
                            f"\tProduct:      {com_ports[i].product}\n",
                            f"\tInterface:    {com_ports[i].interface}\n")

                    model_pos = com_ports[i].serial_number.find("-")
                    if model_pos == -1:
                        continue

                    # My B1 has VID=13587 and PID=2 - Consider using this in the future for greater reliability.
                    model = com_ports[i].serial_number[:model_pos]
                    if model.lower() in supported_models.keys():
                        detected_devices.append({"device": com_ports[i].device, "model": model, "serial_number": com_ports[i].serial_number})

            if len(detected_devices) == 0:
                raise RuntimeError("No supported devices detected")

            if len(detected_devices) > 1:
                error = "Multiple supported devices detected, please select a specific one:\n"
                for devices in detected_devices:
                    error += f"\t{devices["device"]}: {devices["model"]} (Serial No. {devices["serial_number"]})\n"
                raise RuntimeError(error)

            if self._model == "auto":
                self._model = detected_devices[0]["model"].lower()
            elif self._model != detected_devices[0]["model"].lower():
                logging.warning(f"Detected model '{detected_devices[0]["model"]}', but {self._model} was specified. Using model set on command line.")

            self._port = detected_devices[0]["device"]
            self._serial_number = detected_devices[0]["serial_number"]

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

        while True:
            printer_status = self.get_print_status()
            if printer_status["idle"]:
                break

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
        unknown0, idle, progress1, progress2, unknown1, unknown2, error, unknown3, unknown4, unknown5 = struct.unpack(">BBBBBBBBBB", packet.data)

        assert 0 <= idle <= 1, "Unexpected value received for idle"
        return {
            "unknown0": unknown0,
            "idle": bool(idle),
            "progress1": progress1,
            "progress2": progress2,
            "unknown1": unknown1,
            "unknown2": unknown2,
            "error": bool(error),
            "error_code": error,
            "open_paper_compartment": error == 1,
            "unknown3": unknown3,
            "unknown4": unknown4,
            "unknown5": unknown5
        }
