import type {
  ConciseSynthesisOutput,
  SourceCodeReport,
} from "./perfpilot-api";

type Conclusion = ConciseSynthesisOutput["conclusions"][number];

const sourcePathPattern = /(?:[A-Za-z0-9_.-]+[\\/])+(?:[A-Za-z0-9_.-]+\.(?:kt|java|xml|gradle|kts))/gi;

function replaceLiteral(value: string, term: string): string {
  if (!term) return value;
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return value.replace(new RegExp(escaped, "gi"), "对应源码位置");
}

export function redactUnverifiedSourceNarrative(
  value: string,
  source: SourceCodeReport,
): string {
  if (source.match_summary === "strong") return value;
  let redacted = value.replace(sourcePathPattern, "对应源码位置");
  const terms = new Set<string>();
  for (const reference of source.source_refs) {
    terms.add(reference.relative_path);
    const pathParts = reference.relative_path.replace(/\\/g, "/").split("/");
    terms.add(pathParts[pathParts.length - 1] ?? "");
    if (reference.symbol) terms.add(reference.symbol);
  }
  for (const term of [...terms].sort((left, right) => right.length - left.length)) {
    redacted = replaceLiteral(redacted, term);
  }
  return redacted;
}

export function redactUnverifiedConclusion(
  conclusion: Conclusion,
  source: SourceCodeReport,
): Conclusion {
  if (source.match_summary === "strong") return conclusion;
  return {
    ...conclusion,
    problem: redactUnverifiedSourceNarrative(conclusion.problem, source),
    cause: redactUnverifiedSourceNarrative(conclusion.cause, source),
    source_root_cause: redactUnverifiedSourceNarrative(
      conclusion.source_root_cause,
      source,
    ),
    recommendation: redactUnverifiedSourceNarrative(
      conclusion.recommendation,
      source,
    ),
  };
}
