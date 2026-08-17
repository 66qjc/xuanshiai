from app.services.regions import list_cities, list_districts, list_provinces, region_display


def test_region_tree_lookup():
    provinces = list_provinces()
    assert provinces.total == len(provinces.items)
    assert any(item.code == "11" and item.name == "北京市" for item in provinces.items)

    cities = list_cities("11")
    assert any(item.code == "1101" for item in cities.items)

    districts = list_districts("1101")
    assert any(item.code == "110101" and item.name == "东城区" for item in districts.items)


def test_region_display_normalizes_padded_codes() -> None:
    assert region_display("110000", "110100", "110105") == "北京市 朝阳区"
    assert region_display("330000", "330100", None) == "浙江省 杭州市"
