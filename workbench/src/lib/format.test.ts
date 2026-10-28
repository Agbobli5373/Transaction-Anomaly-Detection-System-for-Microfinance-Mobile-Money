import { describe, expect, it } from "vitest";

import { formatDateTime, formatReasonValue, labelOf, maskPhoneNumbers } from "./format";

describe("maskPhoneNumbers", () => {
  it("masks all but the last three digits of a phone-shaped number", () => {
    expect(maskPhoneNumbers("call 233241234567 back")).toBe("call •••••••••567 back");
    expect(maskPhoneNumbers("on +233 241 234 567.")).toBe("on •••••••••567.");
  });

  it("leaves ids, dates, times and amounts alone", () => {
    const text = "T000000012 at 2030-06-01 14:00, GHS 1234.50, wallet w-12345678901";
    expect(maskPhoneNumbers(text)).toBe(text);
  });
});

describe("formatDateTime", () => {
  it("shows UTC, so a page reads the same in every timezone", () => {
    expect(formatDateTime("2030-06-01T14:05:00+00:00")).toBe("01 Jun 2030, 14:05 UTC");
    expect(formatDateTime("2030-06-01T14:05:00+02:00")).toBe("01 Jun 2030, 12:05 UTC");
  });

  it("returns what it cannot read unchanged", () => {
    expect(formatDateTime("later")).toBe("later");
  });
});

describe("Reasons", () => {
  it("states a value as the API gave it, rounded for numbers", () => {
    expect(formatReasonValue(0.9)).toBe("0.9");
    expect(formatReasonValue(12.34567)).toBe("12.346");
    expect(formatReasonValue("Accra")).toBe("Accra");
    expect(formatReasonValue(null)).toBe("none");
  });

  it("makes a code readable", () => {
    expect(labelOf("SIM_SWAP_DRAIN")).toBe("Sim swap drain");
  });
});
