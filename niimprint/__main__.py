import logging
import re

import click
from PIL import Image

from niimprint import BluetoothTransport, PrinterClient, SerialTransport


@click.command("print")
@click.option(
    "-m",
    "--model",
    type=click.Choice(["b1", "b18", "b21", "d11", "d110", "m2"], False),
    default="b21",
    show_default=True,
    help="Niimbot printer model",
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
        assert addr is not None, "--addr argument required for bluetooth connection"
        addr = addr.upper()
        assert re.fullmatch(r"([0-9A-F]{2}:){5}([0-9A-F]{2})", addr), "Bad MAC address"
        transport = BluetoothTransport(addr)
    if conn == "usb":
        port = addr if addr is not None else "auto"
        transport = SerialTransport(port=port)

    if model in ("b1", "b18", "b21"):
        max_width_px = 384
    if model in ("d11", "d110"):
        max_width_px = 96
    if model == "m2":
        # M2 is 300dpi (unlike the ~203dpi/8px-per-mm of the other models here) with a
        # 48mm max print width: 48 / 25.4 * 300 =~ 567px.
        max_width_px = 567

    if model in ("b18", "d11", "d110") and density > 3:
        logging.warning(f"{model.upper()} only supports density up to 3")
        density = 3

    image = Image.open(image)
    if rotate != "0":
        # PIL library rotates counter clockwise, so we need to multiply by -1
        image = image.rotate(-int(rotate), expand=True)
    assert image.width <= max_width_px, f"Image width too big for {model.upper()}"

    # The M2's print head needs real time to fire after the image data is sent;
    # ending the job on the same ~0.3s timing used for the other models here causes
    # it to silently discard the buffered image (a blank label, no error). Poll
    # get_print_status() instead of guessing a fixed delay. See readme.
    printer = PrinterClient(transport)
    printer.print_image(image, density=density, wait_for_status=(model == "m2"))


if __name__ == "__main__":
    print_cmd()
