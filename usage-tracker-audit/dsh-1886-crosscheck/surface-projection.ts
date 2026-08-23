// Stub. The tokenUsage fold must never reach the surface projection. If it
// does, this throws and the run fails loudly instead of printing a number.
// (contextPressure does reach it; this harness folds tokenUsage only.)
export const foldSurfaceProjection = (..._a: unknown[]): never => {
  throw new Error('surface-projection reached from the tokenUsage path')
}
