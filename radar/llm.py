import http.client
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError

from .config import SCHEMA_VERSION
from .models import Contract, Count, EvalResult, Usage
from .queue import Failure
from .storage import write_json_atomic
from .verify import VERSION, Support

MAX_OUTPUT_TOKENS = 1600
JUDGE_OUTPUT_TOKENS = 256
HEADLINE_OUTPUT_TOKENS = 256
HEADLINE_INPUT_TOKENS = 4500
MAX_ARTICLE_BYTES = 12_000
MAX_RESPONSE_BYTES = 256_000
TOKEN_UNIT = Decimal(1_000_000)


class ModelConfig(Contract):
    model: str = Field(min_length=1)
    daily_budget_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    input_usd_per_million: Decimal = Field(gt=0, allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(gt=0, allow_inf_nan=False)
    max_calls: int = Field(default=51, ge=1, le=51)
    max_input_tokens: int = Field(default=20_000, ge=1)


class Judgment(Contract):
    verdicts: list[Support]


class Headline(Contract):
    headline_ko: str = Field(min_length=1, max_length=120)


class BudgetState(Contract):
    date: str
    calls: Count = 0
    estimated_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    input_tokens: Count = 0
    output_tokens: Count = 0


class ModelError(Exception):
    def __init__(self, message, kind: Failure = "api_error"):
        self.kind = kind
        super().__init__(message)


def _post(payload, key):
    connection = http.client.HTTPSConnection("api.openai.com", timeout=60)
    try:
        connection.request(
            "POST",
            "/v1/responses",
            body=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with connection.getresponse() as response:
            if response.status != 200:
                kind = (
                    "api_error"
                    if response.status in {408, 429} or response.status >= 500
                    else "schema_error"
                )
                raise ModelError(f"HTTP {response.status}", kind)
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ModelError("response too large", "schema_error")
            return json.loads(body)
    except ValueError as exc:
        raise ModelError("invalid response JSON", "schema_error") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ModelError(type(exc).__name__) from exc
    finally:
        connection.close()


def _structured_schema(node):
    if isinstance(node, list):
        return [_structured_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    # Apply the portable Structured Outputs subset and validate finer constraints locally.
    constraints = {
        "default",
        "format",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "pattern",
        "title",
    }
    schema = {
        key: (
            {name: _structured_schema(value) for name, value in value.items()}
            if key in {"properties", "$defs"}
            else _structured_schema(value)
        )
        for key, value in node.items()
        if key not in constraints
    }
    if "properties" in schema:
        schema["required"] = list(schema["properties"])
    return schema


class OpenAIEvaluator:
    def __init__(self, config, key, data: Path, now, *, post=_post):
        self.config = config
        self.key = key
        self.path = data / "state" / "llm-budget.json"
        self.day = now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        self.post = post
        prompt_dir = Path(__file__).resolve().parent.parent / "prompts"
        self.prompt = (prompt_dir / "evaluate.md").read_text(encoding="utf-8")
        self.judge_prompt = (prompt_dir / "judge.md").read_text(encoding="utf-8")
        self.headline_prompt = (prompt_dir / "headline.md").read_text(encoding="utf-8")
        self.schema = _structured_schema(EvalResult.model_json_schema())
        self.judge_schema = _structured_schema(Judgment.model_json_schema())
        version_inputs = [
            self.prompt,
            self.judge_prompt,
            config.model,
            SCHEMA_VERSION,
            self.schema,
            self.judge_schema,
            VERSION,
        ]
        self.version = sha256(json.dumps(version_inputs).encode()).hexdigest()[:8]
        self.last_error = None
        self.last_budget_block = False
        self.last_failure = None
        self.last_usage = Usage(input_tokens=0, output_tokens=0)

    def budget_state(self):
        if self.path.exists():
            state = BudgetState.model_validate_json(self.path.read_bytes())
            if state.date == self.day:
                return state
            if state.date > self.day:
                raise ValueError("Budget date is newer than the current KST date")
        return BudgetState(date=self.day)

    def _cost(self, input_tokens, output_tokens):
        return (
            input_tokens * self.config.input_usd_per_million
            + output_tokens * self.config.output_usd_per_million
        ) / TOKEN_UNIT

    def candidate_budget(self):
        budget = self.budget_state()
        remaining = self.config.daily_budget_usd - budget.estimated_usd
        estimate = self._cost(
            2 * self.config.max_input_tokens, MAX_OUTPUT_TOKENS + JUDGE_OUTPUT_TOKENS
        )
        headline = self._cost(HEADLINE_INPUT_TOKENS, HEADLINE_OUTPUT_TOKENS)
        return (
            remaining,
            estimate,
            headline,
            max(0, (self.config.max_calls - budget.calls - 1) // 2),
        )

    @staticmethod
    def _untrusted(source):
        body = (
            json.dumps(source, ensure_ascii=False)
            .replace("<", r"\u003c")
            .replace(">", r"\u003e")
        )
        return f"<untrusted_source>\n{body}\n</untrusted_source>"

    def __call__(self, item, doc, *, corroboration=None):
        self.last_usage = Usage(input_tokens=0, output_tokens=0)
        docs = [doc, corroboration] if corroboration else [doc]
        sources = []
        for index, source in enumerate(docs):
            limit = MAX_ARTICLE_BYTES if len(docs) == 1 else (8_000, 4_000)[index]
            sources.append(
                {
                    "url": str(source.evidence.url),
                    "text": source.text.encode()[:limit].decode(errors="ignore"),
                }
            )
        return self._request(
            self.prompt,
            self.schema,
            EvalResult,
            "radar_evaluation",
            json.dumps(
                {
                    "title": item.title,
                    "url": str(doc.evidence.url),
                    "source_text": self._untrusted(sources),
                },
                ensure_ascii=False,
            ),
            MAX_OUTPUT_TOKENS,
            reserve_judgment=True,
        )

    def judge(self, evidence):
        result = self._request(
            self.judge_prompt,
            self.judge_schema,
            Judgment,
            "radar_judgment",
            self._untrusted(evidence),
            JUDGE_OUTPUT_TOKENS,
        )
        if result is None:
            return None
        if len(result.verdicts) != len(evidence):
            self.last_failure = "schema_error"
            return None
        return result.verdicts

    def headline(self, cards):
        result = self._request(
            self.headline_prompt,
            _structured_schema(Headline.model_json_schema()),
            Headline,
            "radar_headline",
            self._untrusted([" ".join(card.title_ko.split())[:60] for card in cards]),
            HEADLINE_OUTPUT_TOKENS,
            is_headline=True,
        )
        return " ".join(result.headline_ko.split()) if result else None

    def _request(
        self,
        prompt,
        schema,
        contract,
        name,
        body,
        max_output_tokens,
        *,
        reserve_judgment=False,
        is_headline=False,
    ):
        self.last_error = None
        self.last_budget_block = False
        self.last_failure = None
        payload = {
            "model": self.config.model,
            "store": False,
            "instructions": prompt,
            "input": body,
            "max_output_tokens": max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        input_estimate = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
        if input_estimate > self.config.max_input_tokens:
            self.last_failure = "schema_error"
            return None
        estimate = self._cost(input_estimate, max_output_tokens)
        headline = self._cost(HEADLINE_INPUT_TOKENS, HEADLINE_OUTPUT_TOKENS)
        judgment = self._cost(self.config.max_input_tokens, JUDGE_OUTPUT_TOKENS)
        reserve = (0 if is_headline else headline) + (
            judgment if reserve_judgment else 0
        )
        reservation = estimate + reserve
        budget = self.budget_state()
        if (
            budget.calls + (2 if reserve_judgment else 1) > self.config.max_calls
            or budget.estimated_usd + reservation > self.config.daily_budget_usd
        ):
            self.last_budget_block = True
            self.last_failure = "budget"
            return None
        budget.calls += 1
        budget.estimated_usd += reservation
        write_json_atomic(self.path, budget.model_dump(mode="json"))

        cost = estimate
        try:
            response = self.post(payload, self.key)
            usage = response["usage"]
            input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
            if any(
                type(count) is not int or count < 0
                for count in (input_tokens, output_tokens)
            ):
                raise ModelError("invalid usage", "schema_error")
            self.last_usage = Usage(
                input_tokens=self.last_usage.input_tokens + input_tokens,
                output_tokens=self.last_usage.output_tokens + output_tokens,
            )
            budget.input_tokens += input_tokens
            budget.output_tokens += output_tokens
            cost = self._cost(input_tokens, output_tokens)
            if response.get("status") != "completed":
                raise ModelError("incomplete response", "schema_error")
            outputs = [
                content["text"]
                for message in response["output"]
                if message["type"] == "message"
                for content in message["content"]
                if content["type"] == "output_text"
            ]
            if len(outputs) != 1:
                raise ModelError("missing structured output", "schema_error")
            return contract.model_validate_json(outputs[0])
        except (ModelError, ValidationError, KeyError, TypeError) as exc:
            self.last_failure = (
                exc.kind if isinstance(exc, ModelError) else "schema_error"
            )
            self.last_error = (
                str(exc) if isinstance(exc, ModelError) else type(exc).__name__
            )
            return None
        finally:
            # Keep the estimate when the provider did not report usage.
            budget.estimated_usd += cost - reservation
            write_json_atomic(self.path, budget.model_dump(mode="json"))
