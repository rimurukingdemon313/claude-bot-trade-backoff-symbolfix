import { Router, type IRouter } from "express";
import botRouter from "./bot";

const router: IRouter = Router();
router.use(botRouter);

export default router;
