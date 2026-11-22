export type Segment = { text: string; code: boolean };

/** Split a sentence on `backtick` spans so commands can render as code. */
export function splitCode(sentence: string): Segment[] {
  return sentence.split(/`+([^`]+)`+/).flatMap((text, i) => text ? [{ text, code: i % 2 === 1 }] : []);
}
