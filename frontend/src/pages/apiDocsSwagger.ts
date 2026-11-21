import SwaggerUIBundle from "swagger-ui-dist/swagger-ui-bundle.js";
import "swagger-ui-dist/swagger-ui.css";
import "./apiDocs.css";

// validatorUrl null: Swagger otherwise asks validator.swagger.io for a badge,
// and no page may reach off the machine.
export function mountSwagger(el: HTMLElement, url: string): void {
  SwaggerUIBundle({ url, domNode: el, validatorUrl: null, deepLinking: true, docExpansion: "list" });
}
