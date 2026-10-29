//===- OpTimingProfiler.h - per-operation compile time trace ----*- C++ -*-===//
//
// Read-only MLIR action handler that writes each action's duration together
// with the identity of the operation it ran on, as a Chrome Trace file.
//
// Design, coverage boundaries and invariants: docs/design.md.
// File format: docs/trace-format.md.
// Integration and flags: docs/usage.md.
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

/// Chrome Trace writer for MLIR actions. One instance is shared by every
/// context in the process, so all of them share one file and one time origin.
class OpTimingProfiler {
public:
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

  /// False when the trace file could not be opened.
  bool isActive() const { return os != nullptr; }

  /// Bracket `transform()` with two events. Only the string writing is locked,
  /// so concurrent compilation is not serialized by the profiler.
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
  /// `[` plus the format marker: a `ph: "M"` event, which trace viewers ignore
  /// and which tells readers the schema version.
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
      // Identity is written on the opening event only: a rewrite may erase the
      // operation before the closing event is emitted.
      *os << R"(,"args":{"desc":")";
      writeEscaped(getDescription(action));
      *os << '"';
      printActionFields(action);
      printOperationIdentity(action);
      *os << '}';
    }
    *os << '}';
  }

  /// `Action::print()` of the concrete action type, e.g.
  /// "`pass-execution` running `Canonicalizer` on Operation `func.func`".
  llvm::StringRef getDescription(const mlir::tracing::Action &action) {
    description.clear();
    llvm::raw_svector_ostream stream(description);
    action.print(stream);
    // Implementations may end with a newline.
    return llvm::StringRef(description).rtrim();
  }

  /// Structured pass identity, so readers never have to parse `desc`.
  /// `PassExecutionAction::getPass()` is what `print()` renders, so both agree.
  void printActionFields(const mlir::tracing::Action &action) {
    auto *passAction = llvm::dyn_cast<mlir::PassExecutionAction>(&action);
    if (!passAction)
      return;
    const mlir::Pass &pass = passAction->getPass();
    *os << R"(,"pass":")";
    writeEscaped(pass.getName());
    *os << '"';
    // The pipeline option name; empty for passes that register none.
    llvm::StringRef argument = pass.getArgument();
    if (!argument.empty()) {
      *os << R"(,"arg":")";
      writeEscaped(argument);
      *os << '"';
    }
  }

  /// Identity of the first operation carried by the action.
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

  /// Escapes only what can appear in operation, pattern and file names.
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
/// ("-" for stderr). Idempotent per context; leaves a context that already
/// carries an action handler untouched. Returns true when profiling is active.
inline bool installOpTimingIfRequested(mlir::MLIRContext &context) {
  const char *path = std::getenv("OP_TIMING");
  if (!path || !*path)
    return false;

  // Function-local statics: the constructor opens the file, the destructor runs
  // at process exit and appends the closing "]".
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

/// Dialect registry extension; runs when the attached context is constructed,
/// i.e. before parsing and before any pass.
inline void installOpTimingOnContext(mlir::MLIRContext *context) {
  installOpTimingIfRequested(*context);
}

} // namespace optiming

#endif // OPTIMING_OPTIMINGPROFILER_H
