# Wake word models

The `.onnx` files in this directory are openWakeWord's pre-trained models,
downloaded unmodified from its v0.5.1 release:

    https://github.com/dscripka/openWakeWord/releases/tag/v0.5.1

Author: David Scripka and the openWakeWord contributors.

Licence: Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International (CC BY-NC-SA 4.0),
https://creativecommons.org/licenses/by-nc-sa/4.0/

openWakeWord's README gives the reason: its training data includes datasets
with unknown or restrictive licensing. The licence covers every pre-trained
model in the release, the shared feature models `melspectrogram.onnx` and
`embedding_model.onnx` included; the embedding model reimplements Google's
`speech_embedding`, which Google published under Apache-2.0.

These files may be used and shared for non-commercial purposes only, with this
attribution. They are not covered by the licence of the code around them
(openWakeWord's code is Apache-2.0; calliope's is BSD-2-Clause), and no
endorsement by their authors is implied.
