import { copyFile, mkdir, readdir } from 'node:fs/promises';
import { join } from 'node:path';

const destination = 'frontend/vendor';
await mkdir(destination, { recursive: true });
await copyFile('node_modules/@ricky0123/vad-web/dist/bundle.min.js', join(destination, 'vad.bundle.min.js'));
await copyFile('node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js', join(destination, 'vad.worklet.bundle.min.js'));
await copyFile('node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx', join(destination, 'silero_vad_v5.onnx'));
const ortDist = 'node_modules/onnxruntime-web/dist';
const ortAssets = new Set([
  'ort.wasm.min.js',
  'ort-wasm-simd-threaded.mjs',
  'ort-wasm-simd-threaded.wasm',
  'ort-wasm-simd-threaded.jsep.mjs',
  'ort-wasm-simd-threaded.jsep.wasm',
]);
for (const name of await readdir(ortDist)) {
  if (ortAssets.has(name)) {
    await copyFile(join(ortDist, name), join(destination, name));
  }
}
