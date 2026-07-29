import pytest
from pydantic import ValidationError

from app.schemas.location import LocationSharingRequest, LocationUpdateRequest


def test_location_request_validates_coordinate_ranges() -> None:
    request = LocationUpdateRequest(latitude=31.2304, longitude=121.4737, accuracy_m=25)
    assert request.source == "device"


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 91, "longitude": 121},
        {"latitude": 31, "longitude": 181},
        {"latitude": 31, "longitude": 121, "accuracy_m": -1},
    ],
)
def test_location_request_rejects_invalid_values(payload: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        LocationUpdateRequest(**payload)


def test_location_sharing_requires_boolean() -> None:
    assert LocationSharingRequest(enabled=True).enabled is True
