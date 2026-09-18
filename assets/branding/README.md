# assets/branding/

Put a transparent PNG here to watermark every render:

    assets/branding/logo.png

* Present -> used automatically as a small top-right watermark inside the safe zones.
* Absent -> videos render without a watermark. Nothing fails.
* Configure in `config/default.yaml` under `branding:` (enable/disable, position,
  size, opacity, margin). No channel name is required.
