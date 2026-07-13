#!/usr/bin/env python3
"""Build and update the METRPOLA GitHub Pages catalog.

Two modes are supported:
- catalog: download exchange.xml and qty.xml, refresh the product cache,
  download only missing product images, then build catalog-data.js.
- stock: download only qty.xml and rebuild the site from cached exchange.xml.

Required environment variables:
FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_EXCHANGE_PATH, FTP_QTY_PATH.
Optional:
FTP_PORT=21
FTP_IMAGES_DIR=remote image directory (defaults to exchange.xml directory;
               multiple directories may be separated with semicolons)
UPDATE_MODE=catalog|stock
CACHE_EXCHANGE_FILE=data/exchange.xml
CATALOG_META_FILE=data/catalog-meta.json
IMAGES_DIR=images
OUTPUT_FILE=_site/catalog-data.js
PRUNE_IMAGES=1
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REQUIRED_ENV = (
    "FTP_HOST",
    "FTP_USER",
    "FTP_PASSWORD",
    "FTP_EXCHANGE_PATH",
    "FTP_QTY_PATH",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан секрет/параметр {name}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def ftp_download(ftp: FTP, remote_path: str, local_path: Path) -> None:
    """Download a file. Try absolute RETR, then CWD + filename."""
    remote_path = remote_path.strip()
    errors: list[str] = []
    local_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with local_path.open("wb") as target:
            ftp.retrbinary(f"RETR {remote_path}", target.write)
        return
    except Exception as exc:
        errors.append(str(exc))
        local_path.unlink(missing_ok=True)

    path = PurePosixPath(remote_path)
    directory = str(path.parent)
    filename = path.name
    try:
        ftp.cwd("/")
        if directory not in ("", ".", "/"):
            ftp.cwd(directory)
        with local_path.open("wb") as target:
            ftp.retrbinary(f"RETR {filename}", target.write)
        return
    except Exception as exc:
        errors.append(str(exc))
        local_path.unlink(missing_ok=True)

    raise RuntimeError(
        f"Не удалось скачать {remote_path}. Ошибки FTP: {' | '.join(errors)}"
    )


def download_atomic(ftp: FTP, remote_path: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    temporary.unlink(missing_ok=True)
    ftp_download(ftp, remote_path, temporary)
    if temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"FTP вернул пустой файл: {remote_path}")
    temporary.replace(destination)


def parse_number(value: str | None) -> int | float | None:
    if value is None:
        return None
    text = value.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def parse_supply(value: str | None) -> list[dict[str, Any]]:
    text = (value or "").strip()
    if not text:
        return []

    pattern = re.compile(
        r"Поставка\s*([^:;|,]*)\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*:\s*"
        r"([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4})",
        re.IGNORECASE,
    )
    result: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        label = match.group(1).strip() or str(len(result) + 1)
        qty = parse_number(match.group(2))
        date = match.group(3)
        if isinstance(qty, float):
            qty_text = f"{qty:g}"
        else:
            qty_text = str(qty)
        result.append(
            {
                "label": label,
                "qty": qty,
                "date": date,
                "text": f"{qty_text} — {date}",
            }
        )
    if result:
        return result
    return [{"label": "", "qty": None, "date": "", "text": text}]


def load_qty(path: Path) -> dict[str, dict[str, Any]]:
    root = ET.parse(path).getroot()
    result: dict[str, dict[str, Any]] = {}
    for product in root.iter("Товар"):
        product_id = (product.attrib.get("GIUD") or "").strip()
        if not product_id:
            continue
        result[product_id] = {
            "qty": parse_number(product.attrib.get("Qty")),
            "supply": parse_supply(product.attrib.get("Supply")),
        }
    return result


def safe_image_name(value: str | None) -> str:
    name = Path((value or "").strip()).name
    if not name or Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        return ""
    return name


def property_map(product: ET.Element) -> tuple[dict[str, str], list[str]]:
    properties: dict[str, str] = {}
    images: list[str] = []
    props_node = product.find("./Атрибуты/Свойства")
    if props_node is None:
        return properties, images

    for child in props_node:
        if child.tag == "ОсновнаяКартинка":
            main_name = safe_image_name(child.attrib.get("Name"))
            if main_name:
                images.append(main_name)
            for image_node in child.findall("./Картинки/Картинка"):
                image_name = safe_image_name(image_node.attrib.get("Name"))
                if image_name and image_name not in images:
                    images.append(image_name)
            continue
        name = (child.attrib.get("Name") or "").strip()
        value = (child.attrib.get("value") or "").strip()
        if name and value:
            properties[name] = value
    return properties, images


def group_path_for_products(root: ET.Element) -> dict[int, list[str]]:
    paths: dict[int, list[str]] = {}

    def walk(node: ET.Element, current: list[str]) -> None:
        if node.tag == "Группа":
            name = (node.attrib.get("Name") or "").strip()
            current = current + ([name] if name else [])
        elif node.tag == "Товар":
            paths[id(node)] = current
        for child in node:
            if child.tag in ("Группа", "Товар"):
                walk(child, current)

    walk(root, [])
    return paths


def read_exchange_products(
    path: Path, qty_data: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str]]:
    root = ET.parse(path).getroot()
    group_paths = group_path_for_products(root)
    products: list[dict[str, Any]] = []
    all_images: set[str] = set()

    for product in root.iter("Товар"):
        product_id = (product.attrib.get("GIUD") or "").strip()
        if not product_id:
            continue

        properties, images = property_map(product)
        all_images.update(images)
        description_node = product.find("Описание")
        description = ""
        if description_node is not None:
            description = (description_node.attrib.get("Текст") or "").strip()

        retail_node = product.find("./Атрибуты/Цены/Розничная")
        wholesale_node = product.find("./Атрибуты/Цены/Оптовая")
        retail_price = parse_number(
            retail_node.attrib.get("Цена") if retail_node is not None else None
        )
        wholesale_price = parse_number(
            wholesale_node.attrib.get("Цена") if wholesale_node is not None else None
        )

        path_names = group_paths.get(id(product), [])
        fallback_category = path_names[0] if path_names else "Без категории"
        category = properties.get("Вид") or fallback_category
        brand = properties.get("Торговая марка", "")
        collection = properties.get("Коллекция", "")
        mounting = properties.get("Тип монтажа", "")
        current_qty = qty_data.get(product_id, {"qty": None, "supply": []})

        products.append(
            {
                "id": product_id,
                "name": (product.attrib.get("NAME") or "").strip(),
                "description": description,
                "category": category,
                "brand": brand,
                "collection": collection,
                "mounting": mounting,
                "retail_price": retail_price,
                "wholesale_price": wholesale_price,
                "qty": current_qty["qty"],
                "supply": current_qty["supply"],
                "properties": properties,
                "images": images,
            }
        )

    products.sort(
        key=lambda item: (
            str(item["category"]).casefold(),
            str(item["brand"]).casefold(),
            str(item["collection"]).casefold(),
            str(item["name"]).casefold(),
            str(item["id"]),
        )
    )
    return products, all_images


def image_remote_directories(exchange_remote: str) -> list[str]:
    configured = os.environ.get("FTP_IMAGES_DIR", "").strip()
    if configured:
        raw_dirs = re.split(r"[;\n]+", configured)
    else:
        parent = str(PurePosixPath(exchange_remote).parent)
        raw_dirs = [parent, str(PurePosixPath(parent) / "images")]

    result: list[str] = []
    for value in raw_dirs:
        value = value.strip().rstrip("/") or "/"
        if value not in result:
            result.append(value)
    return result


def remote_join(directory: str, filename: str) -> str:
    if directory == "/":
        return "/" + filename
    return str(PurePosixPath(directory) / filename)


def sync_images(
    ftp: FTP,
    exchange_path: Path,
    exchange_remote: str,
    images_dir: Path,
    prune: bool,
) -> dict[str, int]:
    products, required_images = read_exchange_products(exchange_path, {})
    del products
    images_dir.mkdir(parents=True, exist_ok=True)

    existing = {
        path.name
        for path in images_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and path.stat().st_size > 0
    }
    missing = sorted(required_images - existing)
    directories = image_remote_directories(exchange_remote)
    preferred_directory: str | None = None
    downloaded = 0
    failed: list[str] = []

    print(
        f"Изображения: требуется {len(required_images)}, уже есть {len(existing)}, "
        f"нужно скачать {len(missing)}"
    )
    print("Папки поиска изображений на FTP: " + ", ".join(directories))

    for index, filename in enumerate(missing, start=1):
        candidates = list(directories)
        if preferred_directory in candidates:
            candidates.remove(preferred_directory)
            candidates.insert(0, preferred_directory)

        success = False
        last_error = ""
        for directory in candidates:
            remote_path = remote_join(directory, filename)
            try:
                download_atomic(ftp, remote_path, images_dir / filename)
                preferred_directory = directory
                downloaded += 1
                success = True
                break
            except Exception as exc:
                last_error = str(exc)
        if not success:
            failed.append(filename)
            print(f"ПРЕДУПРЕЖДЕНИЕ: не найдено изображение {filename}: {last_error}")
        elif index % 50 == 0 or index == len(missing):
            print(f"Скачано изображений: {downloaded}/{len(missing)}")

    removed = 0
    if prune:
        for path in images_dir.iterdir():
            if (
                path.is_file()
                and path.suffix.lower() in IMAGE_EXTENSIONS
                and path.name not in required_images
            ):
                path.unlink()
                removed += 1

    print(
        f"Изображения готовы: скачано {downloaded}, не найдено {len(failed)}, "
        f"удалено устаревших {removed}"
    )
    return {
        "required": len(required_images),
        "downloaded": downloaded,
        "missing": len(failed),
        "removed": removed,
    }


def read_catalog_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_catalog_meta(path: Path, products_count: int, images_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "catalog_updated_at": now_iso(),
                "products_count": products_count,
                "images_count": images_count,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build(
    exchange_path: Path,
    qty_path: Path,
    output_path: Path,
    catalog_meta_path: Path,
    update_mode: str,
) -> None:
    qty_data = load_qty(qty_path)
    products, all_images = read_exchange_products(exchange_path, qty_data)
    if not products:
        raise RuntimeError("В exchange.xml не найдено ни одного товара")

    catalog_meta = read_catalog_meta(catalog_meta_path)
    data = {
        "meta": {
            "generated_at": now_iso(),
            "stock_updated_at": now_iso(),
            "catalog_updated_at": catalog_meta.get("catalog_updated_at"),
            "products_count": len(products),
            "images_count": len(all_images),
            "update_mode": update_mode,
        },
        "products": products,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.CATALOG_DATA = "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"Готово: {len(products)} товаров -> {output_path}")


def connect_ftp(host: str, port: int, user: str, password: str) -> FTP:
    ftp = FTP()
    ftp.connect(host, port, timeout=90)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Не заданы параметры: " + ", ".join(missing))

    mode = os.environ.get("UPDATE_MODE", "stock").strip().lower()
    if mode not in {"catalog", "stock"}:
        raise RuntimeError("UPDATE_MODE должен быть catalog или stock")

    host = required_env("FTP_HOST")
    user = required_env("FTP_USER")
    password = required_env("FTP_PASSWORD")
    port = int(os.environ.get("FTP_PORT", "21").strip() or "21")
    exchange_remote = required_env("FTP_EXCHANGE_PATH")
    qty_remote = required_env("FTP_QTY_PATH")
    exchange_cache = Path(os.environ.get("CACHE_EXCHANGE_FILE", "data/exchange.xml"))
    catalog_meta = Path(os.environ.get("CATALOG_META_FILE", "data/catalog-meta.json"))
    images_dir = Path(os.environ.get("IMAGES_DIR", "images"))
    output = Path(os.environ.get("OUTPUT_FILE", "_site/catalog-data.js"))

    with tempfile.TemporaryDirectory(prefix="metrpola-") as temp_dir:
        qty_local = Path(temp_dir) / "qty.xml"

        print(f"Режим обновления: {mode}")
        print(f"Подключение к FTP {host}:{port} ...")
        with connect_ftp(host, port, user, password) as ftp:
            if mode == "catalog" or not exchange_cache.exists():
                if mode == "stock" and not exchange_cache.exists():
                    print("Кэш exchange.xml отсутствует: выполняется первоначальная загрузка каталога")
                    mode = "catalog"
                download_atomic(ftp, exchange_remote, exchange_cache)
                print(
                    f"Каталог скачан: {exchange_cache} "
                    f"({exchange_cache.stat().st_size} байт)"
                )

                products_without_qty, all_images = read_exchange_products(exchange_cache, {})
                write_catalog_meta(catalog_meta, len(products_without_qty), len(all_images))

                if env_flag("SYNC_IMAGES", default=True):
                    sync_images(
                        ftp,
                        exchange_cache,
                        exchange_remote,
                        images_dir,
                        prune=env_flag("PRUNE_IMAGES", default=True),
                    )

            download_atomic(ftp, qty_remote, qty_local)
            print(f"Остатки скачаны: {qty_local.stat().st_size} байт")

        build(exchange_cache, qty_local, output, catalog_meta, mode)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise
