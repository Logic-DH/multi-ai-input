from pathlib import Path
from shutil import copy2

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "assets" / "icons"
SOURCE = OUT / "source"
TEMP = Path(r"C:\Users\67697\AppData\Local\Temp")

FILES = {
    "edge": "codex-clipboard-c0dfd836-67d3-47da-973c-bca1c5eea4f1.png",
    "chrome": "codex-clipboard-819e21f7-ff6d-4b22-96e9-f2500719ff7d.png",
    "quark": "codex-clipboard-b0fdc9df-ca54-4c97-8612-8eb3f4afb89c.png",
    "claude": "codex-clipboard-4bd46020-8fb4-46de-94c6-c67eff1dbaf7.png",
    "chatgpt": "codex-clipboard-2d3dd318-0f21-4231-a99b-e3891de7ad5f.png",
    "grok": "codex-clipboard-89ff5b87-91a3-4d09-9ce9-0c07f87927b3.png",
    "gemini": "codex-clipboard-9cc60a72-5fb2-4119-9e2a-6a2bf3d40833.png",
    "deepseek": "codex-clipboard-41541a3a-77c3-4565-9077-a0517d944e4a.png",
    "doubao": "codex-clipboard-d4df65e1-78ca-4e30-b491-f8e33dbbd791.png",
    "qwen": "codex-clipboard-32d87fa5-acbc-40d4-a9f1-a1fbd066aa4a.png",
}

CROPS = {
    "edge": (135, 42, 310, 212),
    "chrome": (52, 63, 398, 387),
    "quark": (58, 58, 390, 390),
    "claude": (0, 110, 118, 252),
    "chatgpt": (60, 86, 166, 194),
    "grok": (82, 26, 462, 360),
    "gemini": (35, 64, 130, 177),
    "deepseek": (0, 165, 128, 300),
    "doubao": (20, 18, 430, 430),
    "qwen": (72, 66, 378, 382),
}


def alpha_from_color(image: Image.Image, mode: str) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = list(rgba.getdata())
    output = []
    for r, g, b, _ in pixels:
        spread = max(r, g, b) - min(r, g, b)
        if mode == "dark":
            alpha = max(0, min(255, (175 - max(r, g, b)) * 4))
        else:
            alpha = max(0, min(255, (spread - 20) * 12))
        output.append((r, g, b, alpha))
    rgba.putdata(output)
    return rgba


def fit_icon(image: Image.Image, size: int = 256, padding: int = 16) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        image = image.crop(bbox)
    target = size - padding * 2
    scale = min(target / image.width, target / image.height)
    image = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    canvas.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
    return canvas


def rounded_square_crop(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 5, fill=255)
    image.putalpha(mask)
    return image


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    SOURCE.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for key, filename in FILES.items():
        source_path = TEMP / filename
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        copy2(source_path, SOURCE / f"{key}.png")
        image = Image.open(source_path).convert("RGBA").crop(CROPS[key])
        if key in {"chatgpt", "doubao"}:
            image = rounded_square_crop(image)
        elif key == "grok":
            image = alpha_from_color(image, "dark")
        else:
            image = alpha_from_color(image, "color")
        icon = fit_icon(image, padding=10 if key in {"edge", "chrome", "quark", "doubao"} else 18)
        icon.save(OUT / f"{key}.png", optimize=True)
        rendered[key] = icon

    sheet = Image.new("RGB", (1000, 430), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (key, icon) in enumerate(rendered.items()):
        col, row = index % 5, index // 5
        x, y = col * 200, row * 215
        preview = icon.resize((132, 132), Image.Resampling.LANCZOS)
        sheet.paste(preview, (x + 34, y + 24), preview)
        draw.text((x + 80, y + 168), key, fill="#0f172a", font=font, anchor="ma")
    sheet.save(ROOT / "ui-previews" / "官方图标裁切核对.png", optimize=True)


if __name__ == "__main__":
    main()
