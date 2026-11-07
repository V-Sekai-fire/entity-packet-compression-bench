#include "entity_predict.h"
int64_t wrap_disp(int32_t a, int32_t b, int32_t c, int32_t d, int32_t lag, int64_t vmax) { return xr_predict_disp_um(a, b, c, d, lag, vmax); }
