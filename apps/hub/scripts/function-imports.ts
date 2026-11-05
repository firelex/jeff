// The packages the hub's function must never import in a way Vercel's file tracer can follow: PGlite (tests and
// local runs only; app/lib/db.server.ts loads it through loadTestOnly, whose package name the tracer cannot see).
// check-build.mjs runs testOnlyImports over every source file of the function, and test/function-imports.test.ts over
// app/lib/db.server.ts, so a static import, an export from, an import('...') or a require('...') of one of these
// fails the build naming the file and line. A type-only reference (import type, typeof import('...')) is erased by the
// compiler and allowed.
//
// This file is loaded by check-build.mjs with Node's own TypeScript type stripping, so it uses only erasable syntax.
import ts from 'typescript';

export const TEST_ONLY_PACKAGES = ['@electric-sql/pglite', 'drizzle-orm/pglite'];

function isTestOnly(specifier: string): boolean {
  return TEST_ONLY_PACKAGES.some((name) => specifier === name || specifier.startsWith(`${name}/`));
}

// Every import of a test-only package in `text` that the compiled code would still make, as "file:line imports name".
export function testOnlyImports(fileName: string, text: string): string[] {
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
  const found: string[] = [];
  const report = (node: ts.Node, specifier: string) => {
    const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    found.push(`${fileName}:${line} imports ${specifier}`);
  };
  const visit = (node: ts.Node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      if (node.importClause?.isTypeOnly !== true && isTestOnly(node.moduleSpecifier.text)) report(node, node.moduleSpecifier.text);
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined && ts.isStringLiteral(node.moduleSpecifier)) {
      if (!node.isTypeOnly && isTestOnly(node.moduleSpecifier.text)) report(node, node.moduleSpecifier.text);
    } else if (ts.isCallExpression(node)) {
      const isImport = node.expression.kind === ts.SyntaxKind.ImportKeyword;
      const isRequire = ts.isIdentifier(node.expression) && node.expression.text === 'require';
      const [first] = node.arguments;
      if ((isImport || isRequire) && first !== undefined && ts.isStringLiteralLike(first) && isTestOnly(first.text)) report(node, first.text);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      const reference = node.moduleReference.expression;
      if (!node.isTypeOnly && ts.isStringLiteral(reference) && isTestOnly(reference.text)) report(node, reference.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

// Every relative import or export in `text` the compiled code would still make whose path has no file ending, as
// "file:line imports ./x". Vercel runs the function as plain Node ES modules, which resolve a relative path exactly as
// written: './schema' is not found where './schema.js' is (the local server and the tests run through a loader that
// tolerates both, so only this check catches it before a deploy).
export function extensionlessImports(fileName: string, text: string): string[] {
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
  const found: string[] = [];
  const check = (node: ts.Node, specifier: string, typeOnly: boolean) => {
    if (typeOnly || !specifier.startsWith('.') || /\.(js|mjs|cjs|json)$/.test(specifier)) return;
    const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    found.push(`${fileName}:${line} imports ${specifier}`);
  };
  const visit = (node: ts.Node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      check(node, node.moduleSpecifier.text, node.importClause?.isTypeOnly === true);
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined && ts.isStringLiteral(node.moduleSpecifier)) {
      check(node, node.moduleSpecifier.text, node.isTypeOnly);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const [first] = node.arguments;
      if (first !== undefined && ts.isStringLiteralLike(first)) check(node, first.text, false);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}
