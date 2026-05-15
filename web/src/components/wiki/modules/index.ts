/**
 * Adaptive page modules — frontend renderers.
 *
 * Each module type has a dedicated React component file. The
 * `ModuleRenderer` dispatcher switches on `module.id` to pick the
 * right component. Adding a new module type requires:
 *   1. adding the catalog entry in the Python `MODULE_CATALOG`
 *   2. creating the per-module React component here
 *   3. wiring it into the dispatcher's switch statement
 *
 * There is intentionally no auto-registration — the explicit switch
 * is the contract. See spec `adaptive-page-modules` for the full
 * vocabulary.
 */

export type { ModuleProps } from "./ModuleRenderer";
export { ModuleRenderer } from "./ModuleRenderer";
