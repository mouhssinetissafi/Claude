import {Config} from '@remotion/cli/config';

// Shared render defaults. The Python side passes --props and the output path.
Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
Config.setPixelFormat('yuv420p');
Config.setCodec('h264');
// Allow serving job assets copied under remotion/public/jobs/<job>/
Config.setPublicDir('./public');
