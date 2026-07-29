# Location and Nearby Users API

All endpoints require `Authorization: Bearer <access_token>` and use the `/api/v1` prefix.

## POST `/users/me/location`

Upload the current device location. The backend stores the latest location and indexes it in Redis GEO. The frontend should call this after permission is granted and then about once every 60 seconds while the nearby map is open.

Request body:

```json
{
  "latitude": 31.2304,
  "longitude": 121.4737,
  "accuracy_m": 25,
  "source": "device"
}
```

| Field | Type | Required | Rules |
|---|---|---:|---|
| `latitude` | number | yes | `-90` to `90` |
| `longitude` | number | yes | `-180` to `180` |
| `accuracy_m` | number/null | no | `0` to `10000` |
| `source` | string | no | 1 to 32 characters, defaults to `device` |

Response:

```json
{
  "enabled": true,
  "latitude": 31.2304,
  "longitude": 121.4737,
  "accuracy_m": 25,
  "updated_at": "2026-07-29T10:20:00"
}
```

## PUT `/users/me/location/sharing`

Enable or disable location sharing.

Request body: `{"enabled": true}` or `{"enabled": false}`.

Disabling sharing immediately removes the user from the online location index. It does not expose the user's location to other users.

## GET `/users/me/location`

Return the current user's location sharing state and latest location.

## DELETE `/users/me/location`

Equivalent to disabling location sharing. This is provided for clients that model location sharing as a resource.

## GET `/users/nearby`

Query nearby online users.

Example:

```text
GET /api/v1/users/nearby?latitude=31.2304&longitude=121.4737&radius_km=20&limit=100
```

| Query | Type | Required | Default | Rules |
|---|---|---:|---:|---|
| `latitude` | number | yes | - | `-90` to `90` |
| `longitude` | number | yes | - | `-180` to `180` |
| `radius_km` | number | no | `20` | greater than `0`, at most `100` |
| `limit` | integer | no | `100` | `1` to `200` |

Response fields:

```json
{
  "items": [
    {
      "user_id": 1002,
      "nickname": "User A",
      "avatar": "https://example.com/avatar.jpg",
      "latitude": 31.23,
      "longitude": 121.475,
      "distance_km": 0.42,
      "online": true,
      "location_updated_at": "2026-07-29T10:20:00"
    }
  ],
  "total": 1,
  "nearest_distance_km": 0.42,
  "radius_km": 20
}
```

The returned marker coordinates are intentionally rounded to a roughly 500m grid. `distance_km` is calculated from the server-side coordinates and is rounded to two decimal places. A user who enables `hide_distance` receives `null` for that user's distance.

Only active users who are online within the existing 90-second presence window, have enabled location sharing, have a visible profile, and are not blocked in either direction are returned.

## Error responses

- `401`: missing or invalid access token.
- `422`: invalid coordinates or query parameters.
- `503`: Redis location index is unavailable.
