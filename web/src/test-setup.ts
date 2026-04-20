import "@testing-library/jest-dom";

// Defensive localStorage shim — idempotent.
//
// Closes RES-214 (Q9) as a forward-compat guard. On 2026-04-15 the web suite
// saw 8 `localStorage.{clear,setItem} is not a function` failures under
// jsdom@27; those resolved upstream, but this shim prevents a future jsdom
// major from reintroducing the regression. The shim installs ONLY when
// `window.localStorage` is absent or its methods are non-callable so real
// jsdom behaviour is preserved whenever it's healthy.
if (
  typeof window !== "undefined" &&
  (!window.localStorage || typeof window.localStorage.clear !== "function")
) {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        store.set(k, String(v));
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
      clear: () => {
        store.clear();
      },
      key: (i: number) => Array.from(store.keys())[i] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}
