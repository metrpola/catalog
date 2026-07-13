#!/usr/bin/env python3
"""Download catalog XML files from FTP and build catalog-data.js.

Uses only the Python standard library. Required environment variables:
FTP_HOST, FTP_USER, FTP_PASSWORD, FTP_EXCHANGE_PATH, FTP_QTY_PATH.
Optional: FTP_PORT (default 21), OUTPUT_FILE (default catalog-data.js).
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
from typing import Any


REQUIRED_ENV = (
    "FTP_HOST",
    "FTP_USER",
    "FTP_PASSWORD",
    "FTP_EXCHANGE_PATH",
    "FTP_QTY_PATH",
)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задан секрет/параметр {name}")
    return value


def ftp_download(ftp: FTP, remote_path: str, local_path: Path) -> None:
    """Download a file. Try an absolute RETR first, then CWD + filename."""
    remote_path = remote_path.strip()
    errors: list[str] = []

    try:
        with local_path.open("wb") as target:
            ftp.retrbinary(f"RETR {remote_path}", target.write)
        return
    except Exception as exc:  # fallback for servers that reject RETR with full path
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

    # Current format: "Поставка 1:52:23.07.2026". Also accepts several entries.
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
        result.append(
            {
                "label": label,
                "qty": qty,
                "date": date,
                "text": f"{qty:g} — {date}" if isinstance(qty, float) else f"{qty} — {date}",
            }
        )
    if result:
        return result

    # Preserve an unfamiliar future format instead of silently dropping it.
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


def property_map(product: ET.Element) -> tuple[dict[str, str], str]:
    properties: dict[str, str] = {}
    image_name = ""
    props_node = product.find("./Атрибуты/Свойства")
    if props_node is None:
        return properties, image_name

    for child in props_node:
        if child.tag == "ОсновнаяКартинка":
            image_name = (child.attrib.get("Name") or "").strip()
            continue
        name = (child.attrib.get("Name") or "").strip()
        value = (child.attrib.get("value") or "").strip()
        if name and value:
            properties[name] = value
    return properties, image_name


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


def load_exchange(path: Path, qty_data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    group_paths = group_path_for_products(root)
    products: list[dict[str, Any]] = []

    for product in root.iter("Товар"):
        product_id = (product.attrib.get("GIUD") or "").strip()
        if not product_id:
            continue

        properties, image_name = property_map(product)
        description_node = product.find("Описание")
        description = ""
        if description_node is not None:
            description = (description_node.attrib.get("Текст") or "").strip()

        retail_node = product.find("./Атрибуты/Цены/Розничная")
        wholesale_node = product.find("./Атрибуты/Цены/Оптовая")
        retail_price = parse_number(retail_node.attrib.get("Цена") if retail_node is not None else None)
        wholesale_price = parse_number(
            wholesale_node.attrib.get("Цена") if wholesale_node is not None else None
        )

        path_names = group_paths.get(id(product), [])
        fallback_category = path_names[0] if path_names else "Без категории"
        category = properties.get("Вид") or fallback_category
        brand = properties.get("Торговая марка", "")
        collection = properties.get("Коллекция", "")
        current_qty = qty_data.get(product_id, {"qty": None, "supply": []})

        products.append(
            {
                "id": product_id,
                "name": (product.attrib.get("NAME") or "").strip(),
                "description": description,
                "category": category,
                "brand": brand,
                "collection": collection,
                "retail_price": retail_price,
                "wholesale_price": wholesale_price,
                "qty": current_qty["qty"],
                "supply": current_qty["supply"],
                "properties": properties,
                "image_name": image_name,
            }
        )

    products.sort(
        key=lambda item: (
            str(item["category"]).casefold(),
            str(item["name"]).casefold(),
            str(item["id"]),
        )
    )
    return products


def build(exchange_path: Path, qty_path: Path, output_path: Path) -> None:
    qty_data = load_qty(qty_path)
    products = load_exchange(exchange_path, qty_data)
    if not products:
        raise RuntimeError("В exchange.xml не найдено ни одного товара")

    data = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "products_count": len(products),
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


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Не заданы параметры: " + ", ".join(missing))

    host = required_env("FTP_HOST")
    user = required_env("FTP_USER")
    password = required_env("FTP_PASSWORD")
    port = int(os.environ.get("FTP_PORT", "21").strip() or "21")
    exchange_remote = required_env("FTP_EXCHANGE_PATH")
    qty_remote = required_env("FTP_QTY_PATH")
    output = Path(os.environ.get("OUTPUT_FILE", "catalog-data.js"))

    with tempfile.TemporaryDirectory(prefix="metrpola-") as temp_dir:
        exchange_local = Path(temp_dir) / "exchange.xml"
        qty_local = Path(temp_dir) / "qty.xml"

        print(f"Подключение к FTP {host}:{port} ...")
        with FTP() as ftp:
            ftp.connect(host, port, timeout=60)
            ftp.login(user, password)
            ftp.set_pasv(True)
            ftp_download(ftp, exchange_remote, exchange_local)
            ftp_download(ftp, qty_remote, qty_local)

        print(
            f"Скачано: exchange.xml {exchange_local.stat().st_size} байт, "
            f"qty.xml {qty_local.stat().st_size} байт"
        )
        build(exchange_local, qty_local, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise
