// SPDX-License-Identifier: MIT

import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const HELPER = "local-announce";
const ANNOUNCEMENT_TIMEOUT_MS = 120_000;
const CLEANUP_GRACE_MS = 20_000;

type HelperStatus = "played" | "cancelled" | "error";

type HelperResult = {
	status: HelperStatus;
	paused: string[];
	resumed: string[];
	skipped: string[];
	warnings: string[];
	audio_seconds?: number;
	error?: string;
};

type ProcessResult = {
	stdout: string;
	stderr: string;
	exitCode: number | null;
	exitSignal: string | null;
	spawnError?: Error & { code?: string };
	stdinError?: Error;
	cancelRequested: boolean;
	timedOut: boolean;
	forcedKill: boolean;
};

type ToolDetails = {
	helper: HelperResult | null;
	exitCode: number | null;
	exitSignal: string | null;
	timedOut: boolean;
	forcedKill: boolean;
};

function isStringArray(value: unknown): value is string[] {
	return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function parseHelperResult(stdout: string): HelperResult {
	const text = stdout.trim();
	if (!text) throw new Error("helper produced no JSON result");

	let value: unknown;
	try {
		value = JSON.parse(text);
	} catch (error) {
		const reason = error instanceof Error ? error.message : String(error);
		throw new Error(`helper produced invalid JSON: ${reason}`);
	}

	if (typeof value !== "object" || value === null || Array.isArray(value)) {
		throw new Error("helper JSON result is not an object");
	}

	const result = value as Record<string, unknown>;
	if (result.status !== "played" && result.status !== "cancelled" && result.status !== "error") {
		throw new Error("helper JSON result has an invalid status");
	}
	for (const field of ["paused", "resumed", "skipped", "warnings"] as const) {
		if (!isStringArray(result[field])) {
			throw new Error(`helper JSON result has an invalid ${field} list`);
		}
	}
	if (
		result.audio_seconds !== undefined &&
		(typeof result.audio_seconds !== "number" || !Number.isFinite(result.audio_seconds) || result.audio_seconds < 0)
	) {
		throw new Error("helper JSON result has an invalid audio_seconds value");
	}
	if (result.error !== undefined && typeof result.error !== "string") {
		throw new Error("helper JSON result has an invalid error value");
	}

	return result as HelperResult;
}

function runHelper(message: string, signal: AbortSignal | undefined): Promise<ProcessResult> {
	const { promise, resolve } = Promise.withResolvers<ProcessResult>();
	let child;
	try {
		child = spawn(HELPER, ["--json"], {
			stdio: ["pipe", "pipe", "pipe"],
			shell: false,
			windowsHide: true,
		});
	} catch (error) {
		resolve({
			stdout: "",
			stderr: "",
			exitCode: null,
			exitSignal: null,
			spawnError: error instanceof Error ? error : new Error(String(error)),
			cancelRequested: false,
			timedOut: false,
			forcedKill: false,
		});
		return promise;
	}

	let stdout = "";
	let stderr = "";
	let spawnError: ProcessResult["spawnError"];
	let stdinError: Error | undefined;
	let cancelRequested = false;
	let timedOut = false;
	let forcedKill = false;
	let closed = false;
	let terminationSent = false;
	let forceKillTimer: NodeJS.Timeout | undefined;

	const requestTermination = (reason: "cancel" | "timeout") => {
		if (closed) return;
		if (reason === "cancel") cancelRequested = true;
		else timedOut = true;
		if (terminationSent) return;

		terminationSent = true;
		child.kill("SIGTERM");
		forceKillTimer = setTimeout(() => {
			if (closed) return;
			forcedKill = true;
			child.kill("SIGKILL");
		}, CLEANUP_GRACE_MS);
	};

	const onAbort = () => requestTermination("cancel");
	signal?.addEventListener("abort", onAbort, { once: true });

	const timeoutTimer = setTimeout(() => requestTermination("timeout"), ANNOUNCEMENT_TIMEOUT_MS);

	child.stdout.setEncoding("utf8");
	child.stdout.on("data", (chunk: string) => {
		stdout += chunk;
	});
	child.stderr.setEncoding("utf8");
	child.stderr.on("data", (chunk: string) => {
		stderr += chunk;
	});
	child.stdin.on("error", (error) => {
		stdinError = error;
	});
	child.on("error", (error) => {
		spawnError = error;
	});
	child.once("close", (exitCode, exitSignal) => {
		closed = true;
		clearTimeout(timeoutTimer);
		clearTimeout(forceKillTimer);
		signal?.removeEventListener("abort", onAbort);
		resolve({
			stdout,
			stderr,
			exitCode,
			exitSignal,
			spawnError,
			stdinError,
			cancelRequested,
			timedOut,
			forcedKill,
		});
	});

	child.stdin.end(message, "utf8");
	return promise;
}

function formatSessions(label: string, sessions: string[]): string | undefined {
	if (sessions.length === 0) return undefined;
	return `${label}: ${sessions.join(", ")}`;
}

function formatHelperContext(result: HelperResult): string[] {
	const lines = [
		formatSessions("Paused", result.paused),
		formatSessions("Resumed", result.resumed),
		formatSessions("Skipped", result.skipped),
	].filter((line): line is string => line !== undefined);
	if (result.warnings.length > 0) {
		lines.push("Warnings:", ...result.warnings.map((warning) => `- ${warning}`));
	}
	return lines;
}

function stderrContext(stderr: string): string | undefined {
	const text = stderr.trim();
	return text ? `Helper diagnostics: ${text}` : undefined;
}

function details(process: ProcessResult, helper: HelperResult | null): ToolDetails {
	return {
		helper,
		exitCode: process.exitCode,
		exitSignal: process.exitSignal,
		timedOut: process.timedOut,
		forcedKill: process.forcedKill,
	};
}

function toolResult(process: ProcessResult) {
	let helper: HelperResult | null = null;
	let protocolError: Error | undefined;
	try {
		helper = parseHelperResult(process.stdout);
	} catch (error) {
		protocolError = error instanceof Error ? error : new Error(String(error));
	}

	if (process.forcedKill) {
		const summary = process.timedOut
			? "Announcement timed out and the helper was forcibly killed after the cleanup grace period; media restoration may be incomplete."
			: "Announcement cancellation required forcibly killing the helper after the cleanup grace period; media restoration may be incomplete.";
		const lines = [summary, ...(helper ? formatHelperContext(helper) : [])];
		const diagnostic = stderrContext(process.stderr);
		if (diagnostic) lines.push(diagnostic);
		return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, helper), isError: true };
	}

	if (process.timedOut) {
		const lines = [
			"Announcement timed out after 120 seconds. The helper was allowed up to 20 seconds to clean up and restore media.",
			...(helper ? formatHelperContext(helper) : []),
		];
		const diagnostic = stderrContext(process.stderr);
		if (diagnostic) lines.push(diagnostic);
		return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, helper), isError: true };
	}

	if (process.cancelRequested) {
		const lines = [
			"Announcement cancelled after waiting for helper cleanup.",
			...(helper ? formatHelperContext(helper) : []),
		];
		if (!helper && protocolError) lines.push(protocolError.message);
		const diagnostic = stderrContext(process.stderr);
		if (diagnostic) lines.push(diagnostic);
		return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, helper), isError: true };
	}

	if (process.spawnError) {
		const code = process.spawnError.code;
		const message =
			code === "ENOENT"
				? "local-announce is not installed or is not on PATH. From the local-tts source directory, run: python3 install.py"
				: `Could not start local-announce: ${process.spawnError.message}`;
		return { content: [{ type: "text" as const, text: message }], details: details(process, helper), isError: true };
	}

	if (protocolError || helper === null) {
		const lines = [`Announcement failed: ${protocolError?.message ?? "helper produced no usable result"}.`];
		if (process.stdinError) lines.push(`Could not finish sending the message: ${process.stdinError.message}`);
		const diagnostic = stderrContext(process.stderr);
		if (diagnostic) lines.push(diagnostic);
		return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, null), isError: true };
	}

	const context = formatHelperContext(helper);
	if (helper.status === "played" && process.exitCode === 0 && process.exitSignal === null) {
		return {
			content: [{ type: "text" as const, text: ["Announcement played.", ...context].join("\n") }],
			details: details(process, helper),
		};
	}

	if (helper.status === "cancelled" && process.exitCode === 130) {
		return {
			content: [{ type: "text" as const, text: ["Announcement cancelled after helper cleanup.", ...context].join("\n") }],
			details: details(process, helper),
			isError: true,
		};
	}

	if (helper.status === "error") {
		const summary = helper.error ? `Announcement failed: ${helper.error}` : "Announcement helper reported an error.";
		const lines = [summary, ...context];
		const diagnostic = stderrContext(process.stderr);
		if (diagnostic) lines.push(diagnostic);
		return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, helper), isError: true };
	}

	const exit = process.exitSignal ? `signal ${process.exitSignal}` : `exit code ${String(process.exitCode)}`;
	const lines = [`Announcement helper returned inconsistent status ${helper.status} with ${exit}.`, ...context];
	const diagnostic = stderrContext(process.stderr);
	if (diagnostic) lines.push(diagnostic);
	return { content: [{ type: "text" as const, text: lines.join("\n") }], details: details(process, helper), isError: true };
}

export default function localAnnounceExtension(pi: ExtensionAPI): void {
	const z = pi.zod;

	pi.registerTool({
		name: "announce",
		label: "Announce",
		description:
			"Use sparingly to speak a concise 1-2 sentence local announcement when user action or a decision is needed, or when important work finishes. Do not use for routine progress, response narration, or long text. This briefly pauses playing local media, plays an attention chime and the message, then safely resumes eligible media. Maximum 300 characters.",
		parameters: z.object({
			message: z.string().trim().min(1).max(300),
		}),
		approval: "exec",
		async execute(_id, params, signal, _onUpdate, _ctx) {
			if (signal?.aborted) {
				return {
					content: [{ type: "text" as const, text: "Announcement cancelled before it started; no helper process was spawned." }],
					details: {
						helper: null,
						exitCode: null,
						exitSignal: null,
						timedOut: false,
						forcedKill: false,
					} satisfies ToolDetails,
					isError: true,
				};
			}

			const message = params.message.trim();
			return toolResult(await runHelper(message, signal));
		},
	});
}
