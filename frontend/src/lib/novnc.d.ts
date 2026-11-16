declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: {
      credentials?: { password?: string };
      shared?: boolean;
      wsProtocols?: string[];
    });
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    disconnect(): void;
  }
}
