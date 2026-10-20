# MultiLink

Windows app that combines hostel Wi-Fi and phone USB tethering to speed up downloads.

- Split downloads across every connected network (chunked HTTP Range requests, resume support)
- Proxy mode: sends browser traffic through the fastest network
- Built-in speed test per network
- Phone tethering (USB) is detected automatically, including phones with no gateway

## Run

Requires Windows and Python 3.10+ (no extra packages needed).

    python multilink.py

Connect the phone with USB tethering on, then press Discover.
