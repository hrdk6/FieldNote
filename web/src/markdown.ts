import { Marked } from "marked";

// Briefs quote collected text, so raw HTML is dropped and only http(s)/mailto links survive.
const SAFE = /^(https?:|mailto:|#|\/)/i;
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const md = new Marked({ gfm: true, breaks: false });
md.use({
  renderer: {
    html() {
      return "";
    },
    link({ href, title, tokens }) {
      const text = this.parser.parseInline(tokens);
      if (!href || !SAFE.test(href)) return text;
      const t = title ? ` title="${esc(title)}"` : "";
      const ext = /^https?:/i.test(href) ? ' target="_blank" rel="noreferrer"' : "";
      return `<a href="${esc(href)}"${t}${ext}>${text}</a>`;
    },
    image({ text }) {
      return esc(text ?? "");
    },
  },
});

export function renderMarkdown(src: string): string {
  return md.parse(src, { async: false }) as string;
}
