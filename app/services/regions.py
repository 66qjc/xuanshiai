"""Read-only province, city and district lookups backed by the bundled JSON file."""

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.schemas.regions import RegionItem, RegionListResponse

_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "p-c-a.json"


def _load_regions() -> list[dict[str, Any]]:
    try:
        with _DATA_PATH.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"区域数据文件不可用: {_DATA_PATH}") from exc
    if not isinstance(data, list):
        raise RuntimeError("区域数据格式必须是数组")
    return data


_REGIONS = _load_regions()


def _response(items: list[dict[str, Any]]) -> RegionListResponse:
    normalized = [RegionItem(code=str(item["code"]), name=str(item["name"])) for item in items]
    return RegionListResponse(items=normalized, total=len(normalized))


def list_provinces() -> RegionListResponse:
    return _response(_REGIONS)


def list_cities(province_code: str) -> RegionListResponse:
    province = next((item for item in _REGIONS if str(item.get("code")) == province_code), None)
    if province is None:
        raise HTTPException(404, detail="省份编码不存在")
    return _response(province.get("children", []))


def list_districts(city_code: str) -> RegionListResponse:
    for province in _REGIONS:
        city = next((item for item in province.get("children", []) if str(item.get("code")) == city_code), None)
        if city is not None:
            return _response(city.get("children", []))
    raise HTTPException(404, detail="城市编码不存在")


def region_name(code: str | None) -> str | None:
    """Resolve one administrative code to its display name."""
    if not code:
        return None
    wanted = str(code)
    for province in _REGIONS:
        if str(province.get("code")) == wanted:
            return str(province.get("name"))
        for city in province.get("children", []):
            if str(city.get("code")) == wanted:
                return str(city.get("name"))
            for district in city.get("children", []):
                if str(district.get("code")) == wanted:
                    return str(district.get("name"))
    return None


def region_display(province_code: str | None, city_code: str | None, district_code: str | None) -> str | None:
    """Build a safe province/city/district label without exposing raw codes."""
    province = str(province_code)[:2] if province_code else None
    city = str(city_code)[:4] if city_code else None
    district = str(district_code)[:6] if district_code else None
    names = [region_name(code) for code in (province, city, district)]
    names = [name for name in names if name and name not in {"市辖区", "县"}]
    return " ".join(dict.fromkeys(names)) or None
