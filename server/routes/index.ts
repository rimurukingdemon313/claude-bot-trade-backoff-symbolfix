import { Router, type IRouter } from "express";
import healthRouter from "./health";
import tradingRouter from "./trading";

const router: IRouter = Router();

router.use(healthRouter);
router.use(tradingRouter);

export default router;
