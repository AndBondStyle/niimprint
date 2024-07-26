import logging
import re

import click
from PIL import Image

from printer import BluetoothTransport, PrinterClient, SerialTransport
from printer_models import supported_models



@click.command("print")
@click.option(
    "-m",
    "--model",
    type=click.Choice(["auto", "b1", "b18", "b21", "d11", "d110"], False),
    default="auto",
    show_default=True,
    help="Niimbot printer model. \"auto\" option only works for USB connections.",
)
@click.option(
    "-c",
    "--conn",
    type=click.Choice(["usb", "bluetooth"]),
    default="usb",
    show_default=True,
    help="Connection type",
)
@click.option(
    "-a",
    "--addr",
    help="Bluetooth MAC address OR serial device path",
)
@click.option(
    "-d",
    "--density",
    type=click.IntRange(1, 5),
    default=5,
    show_default=True,
    help="Print density",
)
@click.option(
    "-r",
    "--rotate",
    type=click.Choice(["0", "90", "180", "270"]),
    default="0",
    show_default=True,
    help="Image rotation (clockwise)",
)
@click.option(
    "-i",
    "--image",
    type=click.Path(exists=True),
    required=True,
    help="Image path",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose logging",
)
def print_cmd(model, conn, addr, density, rotate, image, verbose):
    logging.basicConfig(
        level="DEBUG" if verbose else "INFO",
        format="%(levelname)s | %(module)s:%(funcName)s:%(lineno)d - %(message)s",
    )

    if conn == "bluetooth":
        assert conn is not None, "--addr argument required for bluetooth connection"
        assert model != "auto", "--model argument required for bluetooth connection"
        addr = addr.upper()
        assert re.fullmatch(r"([0-9A-F]{2}:){5}([0-9A-F]{2})", addr), "Bad MAC address"
        transport = BluetoothTransport(addr)
    elif conn == "usb":
        port = addr if addr is not None else "auto"
        transport = SerialTransport(port=port, verbose=verbose)
    else:
        raise RuntimeError("Unsupported connection type specified")

    if model == "auto":
        model = transport._model

    assert model in supported_models.keys(), f"Unsupported model: {model}"

    if density > supported_models[model]["max_density"]:
        logging.warning(f"{model.upper()} only supports density up to {supported_models[model]["max_density"]}")
        density = 3

    image = Image.open(image)
    if rotate != "0":
        # PIL library rotates counter clockwise, so we need to multiply by -1
        image = image.rotate(-int(rotate), expand=True)
    assert image.width <= supported_models[model]["max_width"], f"Image width too big for {model.upper()}"

    printer = PrinterClient(transport)
    printer_status = printer.get_print_status()
    if printer_status["open_paper_compartment"]:
        raise RuntimeError("Printer paper compartment is open, close before proceeding")
    if not printer_status["idle"]:
        raise RuntimeError("Printer is busy")
    if printer_status["error"]:
        logging.warning(f"Printer has error code {printer_status["error_code"]} set - Attempting to ignore.")

    printer.print_image(image, density=density)


if __name__ == "__main__":
    print_cmd()
