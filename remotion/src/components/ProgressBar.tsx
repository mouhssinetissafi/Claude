import React from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';
import type {Theme} from '../themes';

export const ProgressBar: React.FC<{theme: Theme}> = ({theme}) => {
  const frame = useCurrentFrame();
  const {durationInFrames, width} = useVideoConfig();
  const progress = durationInFrames > 1 ? frame / (durationInFrames - 1) : 1;
  const p = theme.progress;
  return (
    <div style={{position: 'absolute', left: 0, bottom: 0, width, height: p.height, background: p.trackColor}}>
      <div style={{width: `${(progress * 100).toFixed(2)}%`, height: '100%', background: p.color}} />
    </div>
  );
};
