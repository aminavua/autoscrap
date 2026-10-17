"""Pricing-table extractor (handles normal and transposed comparison tables)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Tag

from hosting_scraper.config import ExtractionConfig
from hosting_scraper.extract.dom_utils import cell_text, feature_list, has_price_signal
from hosting_scraper.extract.models import Package
from hosting_scraper.extract.normalize import annotate_category, build_package
from hosting_scraper.util.text import clean_text

NAME_HEADER_RE = re.compile(r"plan|package|product|name|hosting|type|service", re.I)
PRICE_HEADER_RE = re.compile(r"price|cost|start|from|monthly|per month|rate", re.I)
FEATURE_HEADER_RE = re.compile(r"feature|include|spec|detail|what", re.I)
PRICE_ROW_RE = re.compile(r"price|cost|start|from|monthly|rate", re.I)


def extract_table_packages(
    html: str,
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    packages: list[Package] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        matrix = [[cell_text(cell) for cell in row.find_all(["th", "td"])] for row in rows]
        matrix = [row for row in matrix if row and any(row)]
        if len(matrix) < 2 or max(len(row) for row in matrix) < 2:
            continue
        if not any(has_price_signal(cell) for row in matrix for cell in row):
            continue
        if _looks_transposed(matrix):
            packages.extend(_parse_transposed(table, matrix, page_url, domain, config))
        else:
            packages.extend(_parse_rows(table, matrix, page_url, domain, config))
    return _dedupe(packages, config.max_packages_per_page)


def _parse_rows(
    table: Tag,
    matrix: list[list[str]],
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    packages: list[Package] = []
    header = matrix[0]
    header_is_header = bool(table.find("th")) or any(
        re.search(NAME_HEADER_RE, c) or re.search(PRICE_HEADER_RE, c) for c in header
    )
    price_col = _find_price_column(header, matrix[1:] if header_is_header else matrix)
    name_col = _find_name_column(header, price_col)
    if price_col is None:
        price_col = _find_price_column([], matrix)
    if price_col is None:
        return packages
    data_rows = matrix[1:] if header_is_header else matrix
    for row in data_rows:
        if price_col >= len(row):
            continue
        price_cell = row[price_col]
        if not has_price_signal(price_cell):
            # Some tables put several offers in one cell; take the first price.
            if not has_price_signal(" ".join(row)):
                continue
            price_cell = next((cell for cell in row if has_price_signal(cell)), "")
            if not price_cell:
                continue
        name = row[name_col] if name_col is not None and name_col < len(row) else ""
        if not name or name == price_cell:
            name = next(
                (
                    cell
                    for index, cell in enumerate(row)
                    if index != price_col and cell and not has_price_signal(cell)
                ),
                "",
            )
        row_text = " | ".join(cell for cell in row if cell)
        features = [
            cell
            for index, cell in enumerate(row)
            if index not in {price_col, name_col} and cell and not has_price_signal(cell)
        ]
        strong = bool(header_is_header and any(PRICE_HEADER_RE.search(c) for c in header))
        package = build_package(
            name=name,
            price_text=price_cell,
            context_text=row_text,
            page_url=page_url,
            domain=domain,
            extractor="dom_tables",
            config=config,
            features=features,
            confidence=None,
            evidence=row_text,
            strong_container=strong,
        )
        if package:
            packages.append(annotate_category(package, page_url))
    return packages


def _looks_transposed(matrix: list[list[str]]) -> bool:
    if len(matrix) < 3 or len(matrix[0]) < 2:
        return False
    header = matrix[0]
    price_rows = [
        row
        for row in matrix[1:]
        if row and PRICE_ROW_RE.search(row[0] or "")
        and sum(1 for cell in row[1:] if has_price_signal(cell)) >= 1
    ]
    if not price_rows:
        return False
    plan_names = [cell for cell in header[1:] if cell and not has_price_signal(cell)]
    return len(plan_names) >= 2


def _parse_transposed(
    table: Tag,
    matrix: list[list[str]],
    page_url: str,
    domain: str,
    config: ExtractionConfig,
) -> list[Package]:
    """Handle tables where plans are columns and features are rows."""
    if len(matrix) < 3:
        return []
    header = matrix[0]
    if len(header) < 2:
        return []
    if header[0] and not FEATURE_HEADER_RE.search(header[0]) and header[0].strip().lower() not in {
        "",
        "plan",
        "plans",
    }:
        # First column should be labels for a column-oriented pricing table.
        label_rows = sum(1 for row in matrix[1:] if row and row[0])
        if label_rows < 2:
            return []
    price_row_index = None
    for index, row in enumerate(matrix[1:], start=1):
        if row and PRICE_ROW_RE.search(row[0]):
            if sum(1 for cell in row[1:] if has_price_signal(cell)) >= 1:
                price_row_index = index
                break
    if price_row_index is None:
        return []
    price_row = matrix[price_row_index]
    if not any(has_price_signal(cell) for cell in price_row[1:]):
        return []
    packages: list[Package] = []
    for column in range(1, min(len(header), len(price_row))):
        name = clean_text(header[column])
        price_cell = price_row[column] if column < len(price_row) else ""
        if not name or not has_price_signal(price_cell):
            continue
        feature_cells: list[str] = []
        for row_index, row in enumerate(matrix[1:], start=1):
            if row_index == price_row_index or column >= len(row):
                continue
            label = row[0] if row else ""
            value = row[column]
            if not value or value.strip() in {"-", "—", "–"}:
                continue
            feature_cells.append(f"{label}: {value}" if label else value)
        features = feature_cells or feature_list(table)
        context = " | ".join([name, *feature_cells])
        package = build_package(
            name=name,
            price_text=price_cell,
            context_text=context,
            page_url=page_url,
            domain=domain,
            extractor="dom_tables",
            config=config,
            features=features,
            confidence=0.72,
            evidence=context,
            strong_container=True,
        )
        if package:
            packages.append(annotate_category(package, page_url))
    return packages


def _find_price_column(header: list[str], data: list[list[str]]) -> int | None:
    for index, cell in enumerate(header):
        if PRICE_HEADER_RE.search(cell or ""):
            return index
    width = max((len(row) for row in data), default=0)
    counts = [0] * width
    for row in data:
        for index, cell in enumerate(row):
            if has_price_signal(cell):
                counts[index] += 1
    if counts and max(counts) > 0:
        return counts.index(max(counts))
    return None


def _find_name_column(header: list[str], price_col: int | None) -> int | None:
    for index, cell in enumerate(header):
        if index == price_col:
            continue
        if NAME_HEADER_RE.search(cell or ""):
            return index
    for index in range(len(header)):
        if index != price_col:
            return index
    return None


def _dedupe(packages: list[Package], limit: int) -> list[Package]:
    best: dict[str, Package] = {}
    for package in packages:
        key = package.dedupe_key()
        current = best.get(key)
        if current is None or package.confidence > current.confidence:
            best[key] = package
    return list(best.values())[:limit]
