function deepFreeze(value) {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value)
    for (const child of Object.values(value)) deepFreeze(child)
  }
  return value
}

export const PROJECT = deepFreeze({
  slug: __PROJECT_SLUG_JSON__,
  title: __PROJECT_TITLE_JSON__,
  productKind: __PROJECT_PRODUCT_KIND_JSON__,
  purpose: __PROJECT_PURPOSE_JSON__,
  primaryUsers: __PROJECT_PRIMARY_USERS_JSON__,
  dangerousWrites: __PROJECT_DANGEROUS_WRITES_JSON__,
})

export const CAPABILITIES = deepFreeze(__PROJECT_CAPABILITIES_JSON__)
export const SCENARIOS = deepFreeze(__PROJECT_SCENARIOS_JSON__)
