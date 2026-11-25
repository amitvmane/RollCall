// Swagger UI for the partner API (/help/api/reference). Kept in its own file:
// the site's CSP refuses inline script. The spec lists only the partner routes
// (api/api_guide.partner_spec); "Authorize" takes an app key from
// Admin → Integrations, and is never stored by this page.
window.addEventListener("load", function () {
  window.SwaggerUIBundle({
    url: "/api/v1/partner/openapi.json",
    dom_id: "#swagger",
    deepLinking: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
    defaultModelsExpandDepth: 0,
  });
});
