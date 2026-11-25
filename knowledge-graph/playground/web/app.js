const form = document.getElementById("query-form");
const question = document.getElementById("question");
const run = document.getElementById("run");
const status = document.getElementById("status");
const error = document.getElementById("error");
const results = document.getElementById("results");
const overlap = document.getElementById("overlap");
const outputs = {
  graph: document.getElementById("graph-output"),
  documents: document.getElementById("documents-output"),
};

const examples = {
  hold: question.defaultValue,
  released: "Which customer site received finished lots built with released sensor lot SL-2408?",
  receipt: "Do these records confirm that shipment SH-905 reached a customer site?",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function fieldLabel(key) {
  const label = key.replaceAll("_", " ").replace(/([a-z])([A-Z])/g, "$1 $2");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function showPaths(paths, container) {
  if (!paths.length) {
    container.append(node("p", "No graph paths were returned for this question. This is not proof that a delivery did not occur.", "empty"));
    return;
  }
  paths.forEach((path, index) => {
    const card = node("article", undefined, "panel path");
    card.append(node("h3", `Path ${index + 1}`, "path-title"));
    Object.entries(path).forEach(([key, value], column) => {
      if (column > 0) {
        const arrow = node("span", "\u2192", "arrow");
        arrow.setAttribute("aria-hidden", "true");
        card.append(arrow);
      }
      const field = node("div", undefined, "node");
      field.append(node("span", fieldLabel(key)), node("strong", String(value ?? "\u2014")));
      card.append(field);
    });
    container.append(card);
  });
}

function originalUrl(record) {
  try {
    const url = new URL(record.url);
    if (url.protocol === "https:" && /^[a-z0-9]{3,24}\.blob\.core\.windows\.net$/.test(url.host)
        && !url.username && !url.password && !url.search && !url.hash) return url.href;
  } catch {
    return null;
  }
  return null;
}

function showEvidence(evidence, container) {
  if (!evidence.length) {
    container.append(node("p", "No original-record excerpts were returned for this question.", "empty"));
    return;
  }
  for (const record of evidence) {
    const card = node("article", undefined, "panel record");
    const heading = node("header", undefined, "record-header");
    heading.append(node("h4", record.recordId), node("span", fieldLabel(record.source.replaceAll("-", " "))));
    card.append(heading, node("p", record.excerpt));
    const url = originalUrl(record);
    if (url) {
      const link = node("a", "Original Blob URL");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      card.append(link);
    }
    container.append(card);
  }
}

function showResult(output, body) {
  const citations = body.evidence.length;
  const metrics = `${citations} Blob ${citations === 1 ? "record" : "records"} \u00b7 ${(body.elapsedMs / 1000).toFixed(1)} s total`;
  const answer = node("section", undefined, "kb-answer");
  answer.append(node("h4", "Answer"), body.answer
    ? node("p", body.answer, "answer-text")
    : node("p", "This knowledge base did not return a written answer. See its retrieved evidence below.", "empty"));
  output.replaceChildren(node("p", body.knowledgeBase, "result-metadata"), node("p", metrics, "badge"), answer);
  if (body.mode === "graph") {
    const paths = node("div", undefined, "paths");
    const count = body.paths.length;
    output.append(node("h4", `${count} graph ${count === 1 ? "path" : "paths"}`), paths);
    showPaths(body.paths, paths);
    if (body.queries.length) {
      const details = node("details", undefined, "panel generated");
      details.append(node("summary", "Generated GQL"), node("pre", body.queries.join("\n\n")));
      output.append(details);
    }
  } else {
    output.append(node("p", "Ranked document excerpts. This KB does not traverse relationships.", "empty"));
  }
  const evidence = node("div", undefined, "evidence");
  output.append(node("h4", "Original Blob evidence"), evidence);
  showEvidence(body.evidence, evidence);
}

function showOverlap(graph, documents) {
  const byRecord = (evidence) => new Map(evidence.map(r => [`${r.source}/${r.recordId}`, r.recordId]));
  const left = byRecord(graph.evidence);
  const right = byRecord(documents.evidence);
  const shared = [...left.keys()].filter(key => right.has(key)).length;
  const only = (from, other) => [...from]
    .filter(([key]) => !other.has(key)).map(([, id]) => id).sort().join(", ") || "none";
  overlap.textContent = `Shared original records: ${shared}. Graph only: ${only(left, right)}. Documents only: ${only(right, left)}.`;
  overlap.hidden = false;
}

async function search(mode, prompt) {
  const output = outputs[mode];
  try {
    const response = await fetch("/api/trace", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: prompt, mode}),
      signal: AbortSignal.timeout(65000),
      redirect: "manual",
    });
    if (response.status === 401 || response.status === 403 || response.type === "opaqueredirect") {
      throw new Error("Sign in with an invited Microsoft Entra account, then reload this page.");
    }
    const isJson = response.headers.get("Content-Type")?.includes("application/json");
    const body = isJson ? await response.json() : null;
    if (!response.ok || body?.mode !== mode) {
      throw new Error(body?.error || "The playground could not reach this knowledge base.");
    }
    showResult(output, body);
    return body;
  } catch (problem) {
    const message = problem.name === "TimeoutError" ? "This search timed out. Try again." : problem.message;
    const notice = node("p", message, "error");
    notice.setAttribute("role", "alert");
    output.replaceChildren(notice);
    return null;
  }
}

for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => {
    question.value = examples[button.dataset.example];
    question.focus();
  });
}

form.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !run.disabled) {
    event.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (run.disabled) return;
  const prompt = question.value.trim();
  error.hidden = true;
  results.hidden = true;
  if (!prompt) {
    error.textContent = "Enter a traceability question.";
    error.hidden = false;
    question.focus();
    return;
  }
  run.disabled = true;
  overlap.hidden = true;
  results.hidden = false;
  status.textContent = "Searching both knowledge bases and generating separate answers\u2026";
  for (const output of Object.values(outputs)) output.replaceChildren(node("p", "Searching\u2026", "empty"));
  try {
    const [graph, documents] = await Promise.all([search("graph", prompt), search("documents", prompt)]);
    if (graph && documents) {
      status.textContent = "Both searches completed.";
      showOverlap(graph, documents);
    } else {
      status.textContent = graph || documents ? "One search did not complete." : "Neither search completed.";
    }
  } finally {
    run.disabled = false;
  }
});
