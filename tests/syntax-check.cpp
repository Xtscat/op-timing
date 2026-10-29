// Compiles the header on its own, so that a standalone configure proves the
// collector needs nothing beyond public MLIR API.  Not linked into anything.
#include "OpTimingProfiler.h"

void optimingSyntaxCheck(mlir::MLIRContext &context) {
  optiming::installOpTimingIfRequested(context);
}
