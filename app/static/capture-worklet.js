/* Capture worklet: hands the main thread 128 ms blocks of float samples.
   Resampling to 16 kHz happens there, in plain JS, so this stays trivial. */
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(128 * Math.max(1, Math.round(sampleRate / 7812)));
    this.offset = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const channel = input[0];
    for (let i = 0; i < channel.length; i++) {
      this.block[this.offset++] = channel[i];
      if (this.offset === this.block.length) {
        this.port.postMessage(this.block);
        this.block = new Float32Array(this.block.length);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('capture', Capture);
