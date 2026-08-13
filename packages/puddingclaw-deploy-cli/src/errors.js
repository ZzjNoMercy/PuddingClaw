export class CliError extends Error {
  constructor(message, { code = "cli_error", exitCode = 2, details } = {}) {
    super(message);
    this.name = "CliError";
    this.code = code;
    this.exitCode = exitCode;
    this.details = details;
  }
}

export function assertArgument(condition, message) {
  if (!condition) throw new CliError(message, { code: "argument_error" });
}
