//===- OpTimingProfiler.h - per-operation compile time trace ----*- C++ -*-===//
//
// Records the duration of MLIR actions together with the identity of the
// operation each action runs on, and writes the result as a Chrome Trace
// Event JSON file (viewable in chrome://tracing, ui.perfetto.dev and
// speedscope.app, and consumable by tools/op-timing-report.py).
//
// Why this exists: neither `-mlir-timing` nor `--profile-actions-to` records
// which operation a pass or a rewrite was working on. Both only carry the
// operation *kind* (`func.func`, `builtin.module`), so a slow pass cannot be
// attributed to a specific operation or source location. MLIR already builds
// the relation we need: every action carries its IR units, and
// `PassExecutionAction` / `ApplyPatternAction` fill them with the operation
// being processed. This file adds the missing half — reading that operation
// and printing it next to the timestamp.
//
// The handler is generic over `tracing::Action`, so every action type MLIR
// emits is recorded without registering it here. Upstream has three:
//
//   `pass-execution`  PassExecutionAction           mlir/lib/Pass/Pass.cpp:521
//   `apply-pattern`   ApplyPatternAction            mlir/lib/Rewrite/PatternApplicator.cpp:195
//   `GreedyPatternRewriteIteration`                 mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:872
//
// Scope: everything inside the MLIR PassManager. Code generation downstream of
// the last MLIR pass (LLVM DAG / ISel / regalloc, or any other target backend)
// emits no MLIR actions and is out of scope by design.
//
// The after-pass verifier is deliberately *not* instrumented, yet it stays
// measurable: `mlir::verify` runs outside every action (mlir/lib/Pass/Pass.cpp:557),
// so it occupies the gap between the `E` of a `pass-execution` action and the
// next event on the same thread. `tools/op-timing-report.py` derives the
// verifier (plus analysis invalidation) from that gap and reports it per pass,
// which keeps pass self times free of it without the host having to disable
// verification.
//
// Integration cost for another compiler built on MLIR: include this header, call
// `installOpTimingOnContext` from a dialect registry extension (or
// `installOpTimingIfRequested` on a context directly), set `OP_TIMING`. No
// symbol here refers to a NorthStar dialect, pass or driver. The hard boundary
// is the Action framework itself: a compiler whose IR does not go through
// `MLIRContext::executeAction` produces no actions and cannot be measured this
// way.
//
// Properties relied upon for long-term maintenance:
//  * read-only: the handler reads `Action::getTag()`, `Action::print()` and the
//    operation name / symbol name / location, then simply calls `transform()`.
//    It never creates, modifies, replaces or skips an operation, so it cannot
//    change what the compiler produces.
//  * inert by default: when `OP_TIMING` is not set the handler is never
//    registered, so `MLIRContext::hasActionHandler()` stays false and
//    `MLIRContext::executeAction` takes its zero-cost branch.
//  * public API only (`MLIRContext::registerActionHandler`, `tracing::Action`),
//    so it does not need to be re-applied when the LLVM submodule moves.
//
//===----------------------------------------------------------------------===//

#ifndef OPTIMING_OPTIMINGPROFILER_H
#define OPTIMING_OPTIMINGPROFILER_H

#include "mlir/IR/Action.h"
#include "mlir/IR/BuiltinAttributes.h"
#include "mlir/IR/Location.h"
#include "mlir/IR/MLIRContext.h"
#include "mlir/IR/Operation.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Support/Threading.h"
#include "llvm/Support/raw_ostream.h"

#include <chrono>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <set>

namespace optiming {

/// Chrome Trace writer for MLIR actions, augmented with the identity of the
/// operation the action applies to.
///
/// One instance is shared by every context in the process, so traces from
/// several contexts land in one file with one common time origin.
class OpTimingProfiler {
public:
  /// Version of the emitted trace format, written into the leading metadata
  /// event so that a reader can reject a file it does not understand.
  static constexpr int kFormatVersion = 1;

  /// Open `path` for writing, or stderr when `path` is "-".
  explicit OpTimingProfiler(llvm::StringRef path)
      : start(std::chrono::steady_clock::now()) {
    if (path == "-") {
      os = &llvm::errs();
      openArray();
      return;
    }
    std::error_code errorCode;
    auto file = std::make_unique<llvm::raw_fd_ostream>(path, errorCode);
    if (errorCode) {
      llvm::errs() << "OP_TIMING: cannot open '" << path
                   << "': " << errorCode.message() << "\n";
      return;
    }
    owned = std::move(file);
    os = owned.get();
    openArray();
  }

  ~OpTimingProfiler() {
    if (os)
      *os << "]\n";
  }

  OpTimingProfiler(const OpTimingProfiler &) = delete;
  OpTimingProfiler &operator=(const OpTimingProfiler &) = delete;

  /// True when the trace file is usable.
  bool isActive() const { return os != nullptr; }

  /// The action handler: bracket `transform()` with two trace events. The
  /// operation identity is read *before* the action runs, because a rewrite may
  /// invalidate the operation it rewrites.
  void operator()(llvm::function_ref<void()> transform,
                  const mlir::tracing::Action &action) {
    if (!os) {
      transform();
      return;
    }
    const auto begin = std::chrono::steady_clock::now();
    {
      std::lock_guard<std::mutex> guard(mutex);
      printEvent(action, "B", begin);
    }
    transform();
    const auto end = std::chrono::steady_clock::now();
    {
      std::lock_guard<std::mutex> guard(mutex);
      printEvent(action, "E", end);
    }
  }

private:
  /// Write `[` followed by the format marker. The marker is a metadata event
  /// (`ph: "M"`), which Chrome Trace consumers ignore, and it is what makes this
  /// file a versioned format rather than an ad-hoc dump: a third-party collector
  /// that emits the same fields can be read by the same report script. See
  /// docs/trace-format.md.
  void openArray() {
    *os << "[\n"
        << R"({"ph":"M","name":"optiming_format","pid":0,)"
        << R"("args":{"schema":"op-timing","version":)" << kFormatVersion
        << "}}";
    first = false;
  }

  void printEvent(const mlir::tracing::Action &action, llvm::StringRef phase,
                  std::chrono::steady_clock::time_point timestamp) {
    const double micros =
        std::chrono::duration<double, std::micro>(timestamp - start).count();
    *os << (first ? "\n" : ",\n");
    first = false;
    *os << R"({"name":")";
    writeEscaped(action.getTag());
    *os << R"(","cat":"op-timing","ph":")" << phase << R"(","pid":0,)"
        << R"("tid":)" << llvm::get_threadid() << R"(,"ts":)" << micros;
    if (phase == "B") {
      *os << R"(,"args":{"desc":")";
      writeEscaped(getDescription(action));
      *os << '"';
      printActionFields(action);
      printOperationIdentity(action);
      *os << '}';
    }
    *os << '}';
  }

  /// `Action::print()` for the concrete action type, e.g.
  /// "`pass-execution` running `Canonicalizer` on Operation `func.func`" or
  /// "`apply-pattern` pattern: LoadOpLowering".
  llvm::StringRef getDescription(const mlir::tracing::Action &action) {
    description.clear();
    llvm::raw_svector_ostream stream(description);
    action.print(stream);
    // `Action::print` implementations may end with a newline.
    return llvm::StringRef(description).rtrim();
  }

  /// Append structured fields for the action kinds that expose them, so that
  /// consumers never have to parse `desc` (which is free-form text produced by
  /// upstream `Action::print()`). `PassExecutionAction::getPass()` is the same
  /// value `print()` renders, so the two never disagree.
  void printActionFields(const mlir::tracing::Action &action) {
    auto *passAction = llvm::dyn_cast<mlir::PassExecutionAction>(&action);
    if (!passAction)
      return;
    const mlir::Pass &pass = passAction->getPass();
    *os << R"(,"pass":")";
    writeEscaped(pass.getName());
    *os << '"';
    // The pipeline option name, e.g. "canonicalize"; empty for passes that do
    // not register one (including the nested-pipeline adaptor).
    llvm::StringRef argument = pass.getArgument();
    if (!argument.empty()) {
      *os << R"(,"arg":")";
      writeEscaped(argument);
      *os << '"';
    }
  }

  /// Append the identity of the first operation carried by the action.
  void printOperationIdentity(const mlir::tracing::Action &action) {
    for (mlir::IRUnit unit : action.getContextIRUnits()) {
      auto *op = llvm::dyn_cast_if_present<mlir::Operation *>(unit);
      if (!op)
        continue;
      *os << R"(,"op":")";
      writeEscaped(op->getName().getStringRef());
      *os << '"';
      if (auto symbol = op->getAttrOfType<mlir::StringAttr>("sym_name")) {
        *os << R"(,"sym":")";
        writeEscaped(symbol.getValue());
        *os << '"';
      }
      if (auto fileLoc = llvm::dyn_cast<mlir::FileLineColLoc>(op->getLoc())) {
        *os << R"(,"loc":")";
        writeEscaped(fileLoc.getFilename());
        *os << ':' << fileLoc.getLine() << ':' << fileLoc.getColumn() << '"';
      } else if (auto nameLoc = llvm::dyn_cast<mlir::NameLoc>(op->getLoc())) {
        *os << R"(,"loc":")";
        writeEscaped(nameLoc.getName().getValue());
        *os << '"';
      }
      return;
    }
  }

  /// Minimal JSON string escaping; the trace is machine generated, so only the
  /// characters that can appear in operation, pattern and file names matter.
  void writeEscaped(llvm::StringRef value) {
    for (char character : value) {
      switch (character) {
      case '"':
        *os << "\\\"";
        break;
      case '\\':
        *os << "\\\\";
        break;
      case '\n':
        *os << "\\n";
        break;
      case '\r':
        *os << "\\r";
        break;
      case '\t':
        *os << "\\t";
        break;
      default:
        if (static_cast<unsigned char>(character) < 0x20)
          *os << '?';
        else
          *os << character;
      }
    }
  }

  std::mutex mutex;
  std::unique_ptr<llvm::raw_fd_ostream> owned;
  llvm::raw_ostream *os = nullptr;
  std::chrono::steady_clock::time_point start;
  llvm::SmallString<128> description;
  bool first = true;
};

/// Install the profiler on `context` when `OP_TIMING` names an output file
/// ("-" for stderr). Returns true when a profiler is active on `context`.
///
/// Idempotent per context. A context that already carries an action handler
/// (for instance because `--profile-actions-to` was passed) is left untouched,
/// so the two facilities never silently disable each other.
inline bool installOpTimingIfRequested(mlir::MLIRContext &context) {
  const char *path = std::getenv("OP_TIMING");
  if (!path || !*path)
    return false;

  // A function-local static: its constructor opens the trace file, and its
  // destructor runs at process exit to write the closing "]".
  static OpTimingProfiler profiler(path);
  static std::set<const void *> installed;
  static std::mutex installMutex;

  if (!profiler.isActive())
    return false;

  std::lock_guard<std::mutex> guard(installMutex);
  if (!installed.insert(&context).second)
    return true;
  if (context.hasActionHandler()) {
    llvm::errs() << "OP_TIMING: this context already has an action handler "
                    "(e.g. --profile-actions-to); per-operation timing is not "
                    "installed\n";
    return false;
  }
  OpTimingProfiler *active = &profiler;
  context.registerActionHandler(
      [active](llvm::function_ref<void()> transform,
               const mlir::tracing::Action &action) {
        (*active)(transform, action);
      });
  return true;
}

/// Dialect-registry extension that installs the profiler on the context the
/// registry is attached to, before any pass runs.
inline void installOpTimingOnContext(mlir::MLIRContext *context) {
  installOpTimingIfRequested(*context);
}

} // namespace optiming

#endif // OPTIMING_OPTIMINGPROFILER_H
