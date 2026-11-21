import { Fragment } from "react";

/** A URL in <code> that wraps after "/", "?", "&" or "," rather than mid-word. */
export default function UrlText({ url }: { url: string }) {
  const pieces = url.split(/(?<=[/?&,])/);
  return (
    <code className="url">
      {pieces.map((piece, i) => (
        <Fragment key={i}>
          {piece}
          {i < pieces.length - 1 && <wbr />}
        </Fragment>
      ))}
    </code>
  );
}
