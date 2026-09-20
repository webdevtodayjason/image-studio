"""PNG encode/decode and upload ingest. Stdlib only.

The farm runs this on Linux, so there is no Mac converter. png is the
only format we decode. Longest edge is 1536.
"""
import struct
import zlib

MAX_EDGE = 1536
MAX_UPLOAD = 25 * 1024 * 1024
ONLY_PNG = (
    "png only. jpeg and webp are not decoded here; save as png and upload that."
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
GIF_MAGIC = (b"GIF87a", b"GIF89a")


class MediaError(ValueError):
    pass


def _chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


def encode_png(width, height, rgb):
    """8-bit RGB PNG, filter 0 on every scanline."""
    if width < 1 or height < 1:
        raise MediaError("empty image")
    bpp = 3
    expect = width * height * bpp
    if len(rgb) != expect:
        raise MediaError("pixel buffer is %d bytes, expected %d" % (len(rgb), expect))
    raw = bytearray()
    row = width * bpp
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y * row:(y + 1) * row])
    return (PNG_MAGIC
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(data, height, stride, bpp):
    out = bytearray(height * stride)
    for y in range(height):
        src = y * (stride + 1)
        dst = y * stride
        ft = data[src]
        row = data[src + 1:src + 1 + stride]
        if len(row) != stride:
            raise MediaError("truncated PNG scanline")
        for x in range(stride):
            a = out[dst + x - bpp] if x >= bpp else 0
            b = out[dst - stride + x] if y else 0
            c = out[dst - stride + x - bpp] if (y and x >= bpp) else 0
            v = row[x]
            if ft == 0:
                pass
            elif ft == 1:
                v = (v + a) & 255
            elif ft == 2:
                v = (v + b) & 255
            elif ft == 3:
                v = (v + ((a + b) // 2)) & 255
            elif ft == 4:
                v = (v + _paeth(a, b, c)) & 255
            else:
                raise MediaError("PNG filter %d is not supported" % ft)
            out[dst + x] = v
    return out


def decode_png(blob):
    """Return (width, height, rgb_bytes). 8-bit gray/RGB/RGBA, no interlace."""
    if not blob.startswith(PNG_MAGIC):
        raise MediaError("not a PNG")
    pos = 8
    width = height = None
    depth = color = interlace = None
    idat = []
    while pos + 12 <= len(blob):
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + n]
        pos += 12 + n
        if tag == b"IHDR":
            if len(data) < 13:
                raise MediaError("bad PNG header")
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", data[:13])
        elif tag == b"IDAT":
            idat.append(data)
        elif tag == b"IEND":
            break
    if not width or not height:
        raise MediaError("PNG has no size")
    if depth != 8:
        raise MediaError("only 8-bit PNG is supported")
    if interlace:
        raise MediaError("interlaced PNG is not supported")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise MediaError("PNG colour type %d is not supported" % color)
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    pixels = _unfilter(raw, height, stride, channels)
    if channels == 3:
        return width, height, bytes(pixels)
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        if channels == 1:
            g = pixels[i]
            rgb[i * 3:i * 3 + 3] = bytes((g, g, g))
        elif channels == 2:
            g, a = pixels[i * 2], pixels[i * 2 + 1]
            rgb[i * 3:i * 3 + 3] = bytes(((g * a) // 255,) * 3)
        else:
            r, g, b, a = pixels[i * 4:i * 4 + 4]
            rgb[i * 3] = (r * a) // 255
            rgb[i * 3 + 1] = (g * a) // 255
            rgb[i * 3 + 2] = (b * a) // 255
    return width, height, bytes(rgb)


def png_size(blob):
    if len(blob) < 24 or not blob.startswith(PNG_MAGIC):
        return None
    w, h = struct.unpack(">II", blob[16:24])
    return w, h


def scale_rgb(rgb, width, height, new_w, new_h):
    if new_w == width and new_h == height:
        return rgb
    out = bytearray(new_w * new_h * 3)
    for y in range(new_h):
        sy0 = y * height // new_h
        sy1 = max(sy0 + 1, (y + 1) * height // new_h)
        for x in range(new_w):
            sx0 = x * width // new_w
            sx1 = max(sx0 + 1, (x + 1) * width // new_w)
            for c in range(3):
                acc = 0
                n = 0
                for sy in range(sy0, sy1):
                    row = sy * width * 3
                    for sx in range(sx0, sx1):
                        acc += rgb[row + sx * 3 + c]
                        n += 1
                out[(y * new_w + x) * 3 + c] = acc // n
    return bytes(out)


def fit_edge(width, height, max_edge=MAX_EDGE):
    long_edge = max(width, height)
    if long_edge <= max_edge:
        return width, height
    scale = max_edge / float(long_edge)
    nw = max(1, int(round(width * scale)))
    nh = max(1, int(round(height * scale)))
    return nw, nh


def detect(blob):
    if blob.startswith(PNG_MAGIC):
        return "png"
    if blob.startswith(JPEG_MAGIC):
        return "jpeg"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:6] in GIF_MAGIC:
        return "gif"
    if blob[:2] == b"BM":
        return "bmp"
    return None


def to_fitted_png(blob):
    """PNG bytes, longest edge at most 1536, aspect kept."""
    if not blob:
        raise MediaError("empty file")
    if len(blob) > MAX_UPLOAD:
        raise MediaError("that file is too large")
    if detect(blob) != "png":
        raise MediaError(ONLY_PNG)
    w, h, rgb = decode_png(blob)
    nw, nh = fit_edge(w, h)
    if (nw, nh) != (w, h):
        rgb = scale_rgb(rgb, w, h, nw, nh)
        w, h = nw, nh
    return encode_png(w, h, rgb), w, h


def split_color_png(width=64, height=64):
    """Half blue, half red. The same shape as the live vision check."""
    rgb = bytearray(width * height * 3)
    mid = width // 2
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 3
            if x < mid:
                rgb[i:i + 3] = b"\x00\x00\xc8"
            else:
                rgb[i:i + 3] = b"\xc8\x00\x00"
    return encode_png(width, height, bytes(rgb))
