// Compiled by a standalone configure to prove the header needs no host headers.
#include "OpTimingProfiler.h"

void optimingSyntaxCheck(mlir::MLIRContext &context) {
  optiming::installOpTimingIfRequested(context);
}
