// Convert the browser's mono stream to 16 kHz PCM in 20 ms packets.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.packet = new Int16Array(320);
    this.index = 0;
    this.phase = 0;
    this.total = 0;
    this.samples = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const sample of channel) {
      this.total += sample;
      this.samples++;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const average = Math.max(-1, Math.min(1, this.total / this.samples));
      this.packet[this.index++] = average * (average < 0 ? 32768 : 32767);
      this.total = 0;
      this.samples = 0;
      if (this.index === this.packet.length) {
        this.port.postMessage(this.packet.buffer, [this.packet.buffer]);
        this.packet = new Int16Array(320);
        this.index = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture", Capture);
