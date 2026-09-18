import React from 'react';
import {AbsoluteFill, Img, Loop, OffthreadVideo, Sequence, interpolate, useCurrentFrame, useVideoConfig, Easing} from 'remotion';
import type {Timeline, TimelineSegment} from '../types';
import type {Theme} from '../themes';
import {resolveAsset, secondsToFrames} from '../lib/assets';

interface SegmentViewProps {
  segment: TimelineSegment;
  assetBase: string;
  theme: Theme;
  durationInFrames: number;
  index: number;
  fadeFrames: number;
}

const cover: React.CSSProperties = {width: '100%', height: '100%', objectFit: 'cover'};

const SegmentView: React.FC<SegmentViewProps> = ({segment, assetBase, theme, durationInFrames, index, fadeFrames}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = durationInFrames > 1 ? Math.min(1, frame / (durationInFrames - 1)) : 1;
  const src = resolveAsset(assetBase, segment.src);

  // Zoom / Ken Burns.
  let scale = 1;
  let translateX = 0;
  let translateY = 0;
  const effect = segment.effect ?? 'none';
  if (segment.type === 'image' || effect === 'kenburns') {
    const amount = theme.kenBurnsScale;
    const direction = index % 2 === 0 ? 1 : -1;
    scale = 1 + amount * (index % 4 < 2 ? progress : 1 - progress);
    translateX = direction * amount * 120 * (progress - 0.5);
    translateY = -direction * amount * 60 * (progress - 0.5);
  } else if (effect === 'zoom_in') {
    scale = 1 + 0.12 * Easing.out(Easing.quad)(progress);
  } else if (effect === 'zoom_out') {
    scale = 1.12 - 0.12 * Easing.out(Easing.quad)(progress);
  } else if (theme.videoDriftScale > 0) {
    const drift = theme.videoDriftScale;
    scale = index % 2 === 0 ? 1 + drift * progress : 1 + drift * (1 - progress);
  }

  const opacity =
    segment.transition === 'fade' && fadeFrames > 0
      ? interpolate(frame, [0, fadeFrames], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})
      : 1;

  const style: React.CSSProperties = {
    ...cover,
    transform: `scale(${scale.toFixed(4)}) translate(${translateX.toFixed(2)}px, ${translateY.toFixed(2)}px)`,
    transformOrigin: 'center center',
  };

  if (segment.type === 'image') {
    return (
      <AbsoluteFill style={{opacity, background: theme.background}}>
        <Img src={src} style={style} />
      </AbsoluteFill>
    );
  }

  const startFrom = secondsToFrames(segment.source_start ?? 0, fps);
  const endAt = segment.source_end !== undefined ? secondsToFrames(segment.source_end, fps) : undefined;
  const sourceFrames = endAt !== undefined ? Math.max(1, endAt - startFrom) : durationInFrames;
  const video = <OffthreadVideo src={src} startFrom={startFrom} endAt={endAt} muted style={style} />;

  return (
    <AbsoluteFill style={{opacity, background: theme.background}}>
      {segment.fallback === 'loop' && sourceFrames < durationInFrames ? (
        <Loop durationInFrames={sourceFrames}>{video}</Loop>
      ) : (
        video
      )}
    </AbsoluteFill>
  );
};

export const MediaLayer: React.FC<{timeline: Timeline; assetBase: string; theme: Theme; fadeSeconds?: number}> = ({
  timeline,
  assetBase,
  theme,
  fadeSeconds = 0.25,
}) => {
  const {fps} = useVideoConfig();
  const fadeFrames = secondsToFrames(fadeSeconds, fps);
  let index = 0;
  return (
    <AbsoluteFill style={{background: theme.background}}>
      {timeline.lines.map((line) =>
        line.segments.map((segment) => {
          const from = secondsToFrames(segment.start, fps);
          const to = secondsToFrames(segment.end, fps);
          const durationInFrames = Math.max(1, to - from);
          const key = `${line.line_id}-${segment.scene_id ?? 'x'}-${from}`;
          const i = index++;
          return (
            <Sequence key={key} from={from} durationInFrames={durationInFrames} layout="none">
              <SegmentView segment={segment} assetBase={assetBase} theme={theme} durationInFrames={durationInFrames} index={i} fadeFrames={fadeFrames} />
            </Sequence>
          );
        }),
      )}
    </AbsoluteFill>
  );
};
