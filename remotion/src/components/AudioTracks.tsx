import React from 'react';
import {Audio, Sequence, interpolate, useVideoConfig} from 'remotion';
import type {Timeline} from '../types';
import {resolveAsset, secondsToFrames} from '../lib/assets';

/** Voice, ducked music bed and SFX. Ducking ramps 0.3s around every narration line. */
export const AudioTracks: React.FC<{timeline: Timeline; assetBase: string}> = ({timeline, assetBase}) => {
  const {fps, durationInFrames} = useVideoConfig();
  const music = timeline.music ?? null;
  const rampFrames = Math.round(fps * 0.3);

  const musicVolume = (frame: number): number => {
    if (!music) {
      return 0;
    }
    const full = music.volume;
    if (!music.ducking) {
      return full;
    }
    const ducked = music.ducking_volume ?? full * 0.4;
    // Find distance to the nearest narration line; inside a line -> ducked.
    let level = full;
    for (const line of timeline.lines) {
      const s = secondsToFrames(line.start, fps);
      const e = secondsToFrames(line.end, fps);
      if (frame >= s && frame <= e) {
        return ducked;
      }
      if (frame < s && s - frame <= rampFrames) {
        level = Math.min(level, interpolate(frame, [s - rampFrames, s], [full, ducked]));
      }
      if (frame > e && frame - e <= rampFrames) {
        level = Math.min(level, interpolate(frame, [e, e + rampFrames], [ducked, full]));
      }
    }
    // Fade out over the last second.
    const tail = interpolate(frame, [durationInFrames - fps, durationInFrames], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
    return level * tail;
  };

  return (
    <>
      {timeline.voice ? <Audio src={resolveAsset(assetBase, timeline.voice)} /> : null}
      {music ? <Audio src={resolveAsset(assetBase, music.src)} loop volume={musicVolume} /> : null}
      {(timeline.sfx ?? []).map((sfx, i) => (
        <Sequence key={`sfx-${i}-${sfx.at}`} from={secondsToFrames(sfx.at, fps)} layout="none">
          <Audio src={resolveAsset(assetBase, sfx.src)} volume={sfx.volume ?? 0.35} />
        </Sequence>
      ))}
    </>
  );
};
