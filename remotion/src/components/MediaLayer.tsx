import React from 'react';
import {AbsoluteFill, Img, Loop, OffthreadVideo, Sequence, interpolate, useCurrentFrame, useVideoConfig, Easing} from 'remotion';
import type {SegmentMotion, Timeline, TimelineSegment} from '../types';
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

const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/**
 * Style for a photo following a planned camera path. The picture is laid out at its
 * cover size (it fills the frame at scale 1) and moved with a transform so the pan
 * stays sub-pixel smooth; the visible window is clamped inside the picture so a bare
 * edge can never show, whatever the planner sent.
 */
export const photoMotionStyle = (motion: SegmentMotion, progress: number, width: number, height: number): React.CSSProperties => {
  const sw = Math.max(1, motion.src_width);
  const sh = Math.max(1, motion.src_height);
  const t = motion.ease === 'linear' ? progress : Easing.inOut(Easing.quad)(progress);
  const scale = Math.max(1, lerp(motion.from.scale, motion.to.scale, t));
  const fx = lerp(motion.from.x, motion.to.x, t);
  const fy = lerp(motion.from.y, motion.to.y, t);
  const base = Math.max(width / sw, height / sh);
  const baseW = sw * base;
  const baseH = sh * base;
  const dw = baseW * scale;
  const dh = baseH * scale;
  const left = clamp(width / 2 - fx * dw, width - dw, 0);
  const top = clamp(height / 2 - fy * dh, height - dh, 0);
  return {
    position: 'absolute',
    left: 0,
    top: 0,
    width: baseW,
    height: baseH,
    transformOrigin: '0 0',
    transform: `translate(${left.toFixed(3)}px, ${top.toFixed(3)}px) scale(${scale.toFixed(5)})`,
    willChange: 'transform',
  };
};

const SegmentView: React.FC<SegmentViewProps> = ({segment, assetBase, theme, durationInFrames, index, fadeFrames}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const progress = durationInFrames > 1 ? Math.min(1, frame / (durationInFrames - 1)) : 1;
  const src = resolveAsset(assetBase, segment.src);

  const opacity =
    segment.transition === 'fade' && fadeFrames > 0
      ? interpolate(frame, [0, fadeFrames], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})
      : 1;

  // Photo with a planned camera move (footage-only photo framings).
  if (segment.type === 'image' && segment.motion) {
    return (
      <AbsoluteFill style={{opacity, background: theme.background, overflow: 'hidden'}}>
        <Img src={src} style={photoMotionStyle(segment.motion, progress, width, height)} />
      </AbsoluteFill>
    );
  }

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
