# Hidden Features

Features that are built but temporarily hidden from the UI. The code is kept in place; re-enable by reverting the listed spots.

## Campaigns (broadcasts)

Hidden on 2026-10-06. Page code (`frontend/src/pages/Campaigns.tsx`), API client and backend are untouched.

To re-enable, restore (search for "hidden — see HIDDEN_FEATURES.md"):
- `frontend/src/components/Layout.tsx` — `Megaphone` import and the `/campaigns` entry in `NAV_ITEMS`
- `frontend/src/pages/Dashboard.tsx` — `Send` import and the "New Broadcast" quick action
- `frontend/src/App.tsx` — `Campaigns` import and the `/campaigns` route (currently redirects to `/dashboard`)
