from pathlib import Path

from PIL import Image, ImageDraw


def draw_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size // 10
    draw.ellipse(
        (margin, margin, size - margin - 1, size - margin - 1),
        fill=(47, 128, 237, 255),
    )
    unit = max(2, size // 8)
    inner = size // 10
    for row in range(2):
        for col in range(2):
            x0 = inner * 3 + col * unit
            y0 = inner * 3 + row * unit
            draw.rounded_rectangle(
                (x0, y0, x0 + unit - 2, y0 + unit - 2),
                radius=max(1, size // 24),
                fill=(255, 255, 255, 255),
            )
    return image


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    sizes = [16, 32, 48, 64, 128, 256]
    images = [draw_icon(size) for size in sizes]
    images[0].save(
        assets / "app.ico",
        format="ICO",
        sizes=[(size, size) for size in sizes],
        append_images=images[1:],
    )
    print(f"已生成 {assets / 'app.ico'}")


if __name__ == "__main__":
    main()

