# Style reference system

Goal: learn *why* highly viewed technology Shorts work, then build a better,
original house style. References are evidence and inspiration, never rules,
and never templates to copy.

## Where things live

| File | Purpose |
|---|---|
| `config/references.yaml` | the registry: candidate channels, roles, trust, thresholds, weights, editorial baseline |
| `cache/references/stats.json` | fetched public statistics per channel (refresh whenever they go stale) |
| `cache/references/style_profile.json` | the weighted house-style profile the pipeline consults |
| `references/annotations/*.json` | human observations of outperforming videos (editing traits) |
| `work/<job>/style_profile.json` | copy of the profile that shaped a specific job |
| `output/<job>/REVIEW.md` | "Style influences" section: which creators/videos influenced the job and why |

## Commands

```bash
export YOUTUBE_API_KEY=...            # free key from Google Cloud (YouTube Data API v3)
python run.py --references refresh    # fetch latest uploads + stats for every registered channel
python run.py --references report     # tiers, weights, active/pruned, reasons
python run.py --references profile    # rebuild the profile from cached stats + annotations
python run.py --references import --references-file stats.json   # no API key: hand-made export
```

Refreshing costs about three API quota units per channel.

## Performance filter (never subscriber counts)

For each channel the latest `sample_size` uploads are fetched; videos at or
under `shorts_max_seconds` count as Shorts. Public data gives views, likes,
comments, duration and publish time, from which the system derives:

* `views_28d_lower_bound`: views on Shorts published inside the window
  (a public lower bound of true 28-day views)
* `median_views` across the recent Shorts sample
* `viral_count`: Shorts over `viral_video_views`
* `outlier_count`: Shorts at `outlier_ratio` x the creator's own median
* `views_per_hour`, `engagement_rate`, and Shorts vs long-form median ratio

Tier rules (`performance:` in the registry):

| Tier | Condition | Weight |
|---|---|---|
| high | ANY strong signal: 5M+ window views, 3+ Shorts over 1M, median 500k+, or 2+ outliers at 3x | 1.0 |
| medium | ANY medium signal: 1M+ window views, 1 viral Short, median 150k+, 1 outlier | 0.5 |
| low | nothing met | 0.0 (excluded) |
| unverified | fewer than `min_shorts_sample` Shorts, stats older than `stale_after_days`, fetch error, or never fetched | 0.0 (excluded) |

Special cases:

* `trust: user_verified_strong` channels keep a small provisional weight
  (`provisional_weight_user_verified`) **only until statistics exist**; after
  that the numbers decide. A stale channel is excluded, whoever supplied it.
* `role: research` channels (TechAltar, ColdFusion) never shape the editing
  style unless their own Shorts tier is high. They remain useful for narrative
  and context, which the writer prompt already encodes as principles.
* Pruning keeps at most `max_active_references` weighted channels; better five
  excellent references than twenty mediocre ones.

Retention, true 28-day analytics and audience data are owner-only and are
never estimated. If a channel cannot be fetched or has too little data it is
excluded with the reason recorded, not guessed.

## Video-level analysis

Within each active channel the outperformers (`video_top_outlier_ratio` x
median) are compared with the creator's baseline on what public metadata can
support: length, title/hook shape (numbers, questions, comparisons), engagement
rate and views per hour. These findings appear in the profile's
`evidence_notes` and in the writer prompt, always with the numbers behind them.

Editing traits that require watching the video (first visual, cut cadence,
caption style, emphasis, structure, payoff) come only from annotation files in
`references/annotations/`. Annotated videos are weighted by channel weight x
outlier score. From them the profile may adjust, within safe bounds:

* timeline cadence (`min/max_segment_seconds` around the weighted mean cut length)
* caption grouping (`max_words_per_caption` 2..5)
* hook guidance text for the writer

The duration policy (45 s minimum, 50-60 s target) is never lowered by references.

## How the profile reaches the video

1. At job start `load_or_build_profile` loads (or builds) the profile.
2. `apply_to_config` pushes evidence-backed knobs into the job config and logs each change.
3. The writer prompt gets a `HOUSE STYLE` block: the editorial principles, the
   weighted evidence, hook notes, and the instruction to do the stronger thing
   whenever a common reference pattern would weaken the Short.
4. `work/<job>/style_profile.json` and the REVIEW.md "Style influences" section
   record which creators and videos influenced the job and why.

With no statistics fetched the profile is `editorial_baseline_only`, the
prompt says so explicitly, and no creator is imitated.

## Re-evaluation

Statistics expire after `stale_after_days`; re-run `--references refresh`
periodically (a cron job is fine). Add or remove channels and change
thresholds or weights in `config/references.yaml`; nothing is hard-coded in
Python. Today's rankings are never baked in.
