# Nearby Map Development Guide

This document is for both backend and frontend developers implementing the nearby online-user map.

## Runtime flow

1. The frontend asks the browser or app for location permission.
2. After permission is granted, call `POST /api/v1/users/me/location`.
3. The backend stores the latest coordinate in `user_profile` and writes the user to Redis GEO key `location:online:users`.
4. Call `GET /api/v1/users/nearby` to render the map.
5. While the map is open, upload location about every 60 seconds and refresh nearby users about every 30 to 60 seconds.
6. On logout, leaving the map, or turning sharing off, call `DELETE /api/v1/users/me/location` when appropriate.

## Frontend integration

Use the device location coordinate system expected by the configured map provider. The existing profile schema documents the stored coordinates as GCJ-02. Do not silently mix WGS-84, GCJ-02, and BD-09 coordinates.

Recommended UI states:

- Permission not requested: show the location permission action.
- Permission denied: show the city or an empty map and allow retry from system settings.
- Sharing disabled: do not query nearby users until the user enables sharing.
- Loading: keep the existing map and show a non-blocking loading state.
- No results: show the current location with an empty nearby result state.
- Results: use `items` as markers and `nearest_distance_km` for the nearest-user summary.

The marker latitude and longitude are privacy-rounded. They must not be used as the exact position of another user. Use `distance_km` for the distance label and render `null` as a hidden-distance state.

## Backend rules

- The existing presence window is 90 seconds. A stale heartbeat is not online.
- Redis GEO is the online query index; MySQL is the durable latest-location projection.
- No location history is recorded by this feature.
- `user_block`, `user_privacy.hide_distance`, `user_privacy.hide_online_status`, and `user_privacy.show_profile` are applied by the nearby endpoint.
- The server never returns an unrounded third-party coordinate.
- Do not add exact coordinates to logs, analytics events, notifications, or public profile responses.

## Database compatibility

Location fields are on `user_profile` and are added by the existing idempotent database initializer for older databases:

```text
location_source
location_updated_at
location_precision
location_consent
location_visible
```

The existing `latitude` and `longitude` fields are reused. No new dependency is required; Redis GEO commands are provided by the existing Redis client.

## Testing checklist

- Coordinates outside their valid ranges return `422`.
- A location upload enables sharing and updates Redis GEO.
- Turning sharing off removes the Redis GEO member.
- Stale, hidden, blocked, and incomplete users are excluded.
- Distance is sorted ascending and hidden distance returns `null`.
- Returned third-party coordinates are rounded and never equal to the stored exact coordinate by default.
- Redis failure returns `503` instead of claiming that the user is visible.
