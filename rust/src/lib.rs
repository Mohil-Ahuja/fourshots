//! An independent reimplementation of the NPCI/RBI constraint lattice.
//!
//! Why this exists
//! ---------------
//! This is not a faster backend. The benchmark runs in seconds, so there is no
//! performance problem to solve, and swapping implementations by environment
//! would mean behaviour differing depending on whether an extension happened to
//! compile on a given machine — a real risk bought with no benefit.
//!
//! It exists to be a *second opinion*. The regulatory gate is the part of the
//! system where a silent error is most expensive: it decides whether a debit
//! against someone's account is permitted at all. Everywhere else a bug shows
//! up as a worse number; here it shows up as an illegal attempt that the
//! benchmark still counts as fine.
//!
//! So the rules are written twice, from the circulars rather than from the
//! Python, and `tests/test_differential.py` generates random inputs and asserts
//! both implementations agree. Two independent implementations that agree on a
//! hundred thousand cases are evidence; one implementation that passes its own
//! tests is an assertion.
//!
//! Python remains the only execution path. Nothing here is called in
//! production, and a checkout without a Rust toolchain simply skips the
//! differential test.
//!
//! Deliberately primitive at the boundary
//! --------------------------------------
//! Everything crossing the FFI is an integer or a bool — minutes since midnight
//! IST, paise, counts. Marshalling timezone-aware datetimes across two runtimes
//! is its own source of bugs, and a differential test whose disagreements come
//! from marshalling teaches nothing about the rules.

use pyo3::prelude::*;

/// UPI peak windows in minutes since midnight IST: 10:00–13:00 and 17:00–21:30.
///
/// AutoPay mandates do not execute inside these. Half-open [start, end), so
/// 10:00 is blocked and 13:00 is allowed — the boundary convention is itself
/// something the differential test checks, because an off-by-one here is
/// exactly the kind of error that never shows up in aggregate numbers.
const PEAK_WINDOWS: [(u32, u32); 2] = [(600, 780), (1020, 1290)];

const MINUTES_PER_DAY: u32 = 1440;

/// Violation codes. Kept as integers across the boundary and mapped back to the
/// Python enum on the far side, so neither implementation depends on the
/// other's spelling.
pub const V_ATTEMPT_BUDGET_EXHAUSTED: u32 = 1;
pub const V_INSUFFICIENT_NOTICE: u32 = 2;
pub const V_PEAK_WINDOW: u32 = 3;
pub const V_CUSTOMER_OPTED_OUT: u32 = 4;
pub const V_AFA_REQUIRED: u32 = 5;
pub const V_MANDATE_NOT_ACTIVE: u32 = 6;

/// True if a debit may legally execute at this minute of the day.
pub fn is_in_execution_window(minute_of_day: u32) -> bool {
    let m = minute_of_day % MINUTES_PER_DAY;
    !PEAK_WINDOWS
        .iter()
        .any(|&(start, end)| m >= start && m < end)
}

/// Minutes to wait from `minute_of_day` before a debit could legally execute.
///
/// Zero when the instant is already legal, so callers can add it
/// unconditionally without first testing.
pub fn minutes_until_legal(minute_of_day: u32) -> u32 {
    let m = minute_of_day % MINUTES_PER_DAY;
    for &(start, end) in PEAK_WINDOWS.iter() {
        if m >= start && m < end {
            return end - m;
        }
    }
    0
}

/// True if this debit needs Additional Factor of Authentication.
///
/// Strictly greater than the threshold: a debit exactly at INR 15,000 does not
/// require AFA. That boundary is worth stating because getting it wrong costs a
/// customer an authentication step they are not owed, and it is invisible in
/// any aggregate metric.
pub fn afa_required(amount_paise: i64, threshold_paise: i64) -> bool {
    amount_paise > threshold_paise
}

/// Every way a proposed attempt can be illegal.
///
/// Returns all violations rather than the first, matching the Python: an audit
/// log recording only the first tripped rule tells a reader less than the
/// complete reason.
#[allow(clippy::too_many_arguments)]
pub fn check_legality(
    attempts_used: u32,
    max_attempts: u32,
    notice_minutes: i64,
    required_notice_minutes: i64,
    execute_minute_of_day: u32,
    mandate_active: bool,
    customer_opted_out: bool,
    amount_paise: i64,
    afa_threshold_paise: i64,
    afa_obtained: bool,
) -> Vec<u32> {
    let mut violations = Vec::new();

    if attempts_used >= max_attempts {
        violations.push(V_ATTEMPT_BUDGET_EXHAUSTED);
    }
    if !mandate_active {
        violations.push(V_MANDATE_NOT_ACTIVE);
    }
    if customer_opted_out {
        violations.push(V_CUSTOMER_OPTED_OUT);
    }
    if notice_minutes < required_notice_minutes {
        violations.push(V_INSUFFICIENT_NOTICE);
    }
    if !is_in_execution_window(execute_minute_of_day) {
        violations.push(V_PEAK_WINDOW);
    }
    if afa_required(amount_paise, afa_threshold_paise) && !afa_obtained {
        violations.push(V_AFA_REQUIRED);
    }

    violations
}

// ---------------------------------------------------------------------------
// Python bindings
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(name = "is_in_execution_window")]
fn py_is_in_execution_window(minute_of_day: u32) -> bool {
    is_in_execution_window(minute_of_day)
}

#[pyfunction]
#[pyo3(name = "minutes_until_legal")]
fn py_minutes_until_legal(minute_of_day: u32) -> u32 {
    minutes_until_legal(minute_of_day)
}

#[pyfunction]
#[pyo3(name = "afa_required")]
fn py_afa_required(amount_paise: i64, threshold_paise: i64) -> bool {
    afa_required(amount_paise, threshold_paise)
}

#[pyfunction]
#[pyo3(name = "check_legality")]
#[allow(clippy::too_many_arguments)]
fn py_check_legality(
    attempts_used: u32,
    max_attempts: u32,
    notice_minutes: i64,
    required_notice_minutes: i64,
    execute_minute_of_day: u32,
    mandate_active: bool,
    customer_opted_out: bool,
    amount_paise: i64,
    afa_threshold_paise: i64,
    afa_obtained: bool,
) -> Vec<u32> {
    check_legality(
        attempts_used,
        max_attempts,
        notice_minutes,
        required_notice_minutes,
        execute_minute_of_day,
        mandate_active,
        customer_opted_out,
        amount_paise,
        afa_threshold_paise,
        afa_obtained,
    )
}

#[pymodule]
fn fourshots_rules(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_is_in_execution_window, m)?)?;
    m.add_function(wrap_pyfunction!(py_minutes_until_legal, m)?)?;
    m.add_function(wrap_pyfunction!(py_afa_required, m)?)?;
    m.add_function(wrap_pyfunction!(py_check_legality, m)?)?;
    m.add("V_ATTEMPT_BUDGET_EXHAUSTED", V_ATTEMPT_BUDGET_EXHAUSTED)?;
    m.add("V_INSUFFICIENT_NOTICE", V_INSUFFICIENT_NOTICE)?;
    m.add("V_PEAK_WINDOW", V_PEAK_WINDOW)?;
    m.add("V_CUSTOMER_OPTED_OUT", V_CUSTOMER_OPTED_OUT)?;
    m.add("V_AFA_REQUIRED", V_AFA_REQUIRED)?;
    m.add("V_MANDATE_NOT_ACTIVE", V_MANDATE_NOT_ACTIVE)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Rust-side tests: the boundaries, stated against the circulars.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peak_boundaries_are_half_open() {
        assert!(is_in_execution_window(599)); // 09:59 legal
        assert!(!is_in_execution_window(600)); // 10:00 blocked
        assert!(!is_in_execution_window(779)); // 12:59 blocked
        assert!(is_in_execution_window(780)); // 13:00 legal
        assert!(is_in_execution_window(1019)); // 16:59 legal
        assert!(!is_in_execution_window(1020)); // 17:00 blocked
        assert!(!is_in_execution_window(1289)); // 21:29 blocked
        assert!(is_in_execution_window(1290)); // 21:30 legal
    }

    #[test]
    fn midnight_and_late_night_are_legal() {
        assert!(is_in_execution_window(0));
        assert!(is_in_execution_window(1439));
    }

    #[test]
    fn waiting_rolls_to_the_end_of_the_blocking_window() {
        assert_eq!(minutes_until_legal(660), 120); // 11:00 -> 13:00
        assert_eq!(minutes_until_legal(1080), 210); // 18:00 -> 21:30
        assert_eq!(minutes_until_legal(800), 0); // already legal
    }

    #[test]
    fn waiting_always_reaches_a_legal_minute() {
        for m in 0..MINUTES_PER_DAY {
            assert!(is_in_execution_window(m + minutes_until_legal(m)));
        }
    }

    #[test]
    fn afa_threshold_is_exclusive() {
        assert!(!afa_required(1_500_000, 1_500_000)); // exactly INR 15,000
        assert!(afa_required(1_500_001, 1_500_000));
    }

    #[test]
    fn every_violation_is_reported_not_just_the_first() {
        let violations = check_legality(4, 4, 60, 1440, 660, false, true, 5_000_000, 1_500_000, false);
        assert_eq!(violations.len(), 6);
    }

    #[test]
    fn a_clean_attempt_has_no_violations() {
        let violations = check_legality(1, 4, 1500, 1440, 480, true, false, 49_900, 1_500_000, false);
        assert!(violations.is_empty());
    }
}
