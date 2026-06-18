"""Generate QR codes (for phone scanning) and Code 128 barcodes (for scanners)."""
from io import BytesIO

import qrcode
import qrcode.constants


def qr_png_bytes(data: str) -> bytes:
    """Return PNG bytes of a QR code encoding `data` (usually a tracking URL)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def code128_png_bytes(text: str) -> bytes:
    """Return PNG bytes of a Code 128 barcode encoding `text`."""
    import barcode
    from barcode.writer import ImageWriter

    code = barcode.get("code128", text, writer=ImageWriter())
    buf = BytesIO()
    code.write(buf, options={
        "module_width": 0.3,
        "module_height": 12.0,
        "font_size": 8,
        "text_distance": 3.0,
        "quiet_zone": 4.0,
    })
    return buf.getvalue()
