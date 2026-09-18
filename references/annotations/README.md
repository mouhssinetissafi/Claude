# references/annotations/

Human observations of *outperforming* Shorts. The public YouTube API only gives
views, likes, comments, duration and titles; the editing traits that actually
matter (cut rate, first visual, caption style, emphasis, structure, payoff) must
be observed by a person watching the video. Nothing in here is ever inferred
automatically, so an empty folder simply means "editorial baseline only".

One file per channel, named after the handle (`mkbhd.json`). Files starting
with `_` are ignored. Format (see `_template.json`):

```json
{
  "handle": "mkbhd",
  "observed_by": "your name",
  "observed_at": "2026-09-18",
  "videos": [
    {
      "video_id": "abc123XYZ",
      "hook_type": "claim",              // claim | question | contrast | demo | reveal | problem
      "first_visual_seconds": 0.4,
      "avg_cut_seconds": 1.8,
      "caption_style": "word",           // word | phrase | none
      "emphasis": "selective",           // selective | heavy | none
      "visual_density": "high",          // low | medium | high
      "structure": "setup -> three escalating details -> payoff",
      "payoff": "reveals the trade-off in the last line",
      "notes": "why this one outperformed the channel baseline"
    }
  ]
}
```

Annotated videos are weighted by the channel's performance weight times the
video's outlier score, so a viral Short from a high-tier channel counts far more
than an average one. Only channels that are active in the profile contribute.
