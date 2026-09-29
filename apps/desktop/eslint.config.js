// Configuración de ESLint (flat) de la interfaz de Faro.
import js from "@eslint/js";
import i18next from "eslint-plugin-i18next";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

// Atributos JSX técnicos que nunca contienen texto visible para el usuario.
// `aria-label`, `title`, `alt`, `placeholder` y el texto de los elementos sí se revisan.
const technicalJsxAttributes = [
  "className",
  "style",
  "type",
  "key",
  "id",
  "width",
  "height",
  "to",
  "href",
  "role",
  "lang",
  "name",
  "htmlFor",
  "autoComplete",
  "variant",
  "size",
  "side",
  "align",
  "aria-hidden",
  "aria-current",
  "aria-controls",
  "aria-live",
  "aria-describedby",
  "aria-labelledby",
  "data-.*",
  "sectionId",
];

export default defineConfig([
  globalIgnores(["dist", "coverage", "src-tauri"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.strictTypeChecked,
      tseslint.configs.stylisticTypeChecked,
      reactHooks.configs.flat.recommended,
      jsxA11y.flatConfigs.recommended,
      i18next.configs["flat/recommended"],
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "i18next/no-literal-string": [
        "error",
        {
          mode: "jsx-only",
          "jsx-attributes": { exclude: technicalJsxAttributes },
        },
      ],
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
  {
    files: ["*.config.ts"],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["**/*.js"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node },
  },
]);
