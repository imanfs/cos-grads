import torch
import torch.nn.functional as F
import numpy as np

"""
Define task metrics, loss functions and model trainer here.
"""
@torch.jit.script
def dice_loss(inputs, labels):
    """Compute the DICE loss, similar to generalized IOU for masks.

    Parameters
    ----------
    inputs : Tensor
        The predictions for each example.
    labels : Tensor
        A float tensor with the same shape as inputs. Stores the binary classification label
        for each element in inputs (0 for the negative class and 1 for the positive class).

    Returns
    -------
    Tensor
        Single-element loss tensor
    """
    inputs = inputs.sigmoid()
    numerator = 2 * (inputs * labels).sum(-1)
    denominator = inputs.sum(-1) + labels.sum(-1)
    loss = 1 - (numerator + 1) / (denominator + 1)
    return loss.sum() / len(inputs)


@torch.jit.script
def mask_ce_loss(inputs, labels):
    """Computes cross entropy loss for masks.

    Parameters
    ----------
    inputs: Tensor
            A float tensor of arbitrary shape representing the predictions for each example.
    labels: Tensor
        A float tensor with the same shape as inputs. Stores the binary classification label
        for each element in inputs (0 for the negative class and 1 for the positive class).

    Returns
    -------
    Tensor
        Single-element loss tensor
    """
    loss = F.binary_cross_entropy_with_logits(inputs, labels, reduction="none")
    # find the mean loss for each mask
    loss = loss.mean(1)

    # take the average over all masks
    return loss.sum() / len(inputs)


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def model_fit(x_pred, x_output, task_type):
    device = x_pred.device

    # binary mark to mask out undefined pixel space
    binary_mask = (torch.sum(x_output, dim=1) != 0).float().unsqueeze(1).to(device)

    if task_type == 'semantic':
        # semantic loss: depth-wise cross entropy
        loss = F.nll_loss(x_pred, x_output, ignore_index=-1)

    if task_type == 'depth':
        # depth loss: l1 norm
        loss = torch.sum(torch.abs(x_pred - x_output) * binary_mask) / torch.nonzero(binary_mask, as_tuple=False).size(0)

    if task_type == 'normal':
        # normal loss: dot product
        loss = 1 - torch.sum((x_pred * x_output) * binary_mask) / torch.nonzero(binary_mask, as_tuple=False).size(0)

    return loss


# New mIoU and Acc. formula: accumulate every pixel and average across all pixels in all images
class ConfMatrix(object):
    def __init__(self, num_classes):
        self.num_classes = num_classes
        self.mat = None

    def update(self, pred, target):
        n = self.num_classes
        if self.mat is None:
            self.mat = torch.zeros((n, n), dtype=torch.int64, device=pred.device)
        with torch.no_grad():
            k = (target >= 0) & (target < n)
            inds = n * target[k].to(torch.int64) + pred[k]
            self.mat += torch.bincount(inds, minlength=n ** 2).reshape(n, n)

    def get_metrics(self):
        h = self.mat.float()
        acc = torch.diag(h).sum() / h.sum()
        iu = torch.diag(h) / (h.sum(1) + h.sum(0) - torch.diag(h))
        return torch.mean(iu).item(), acc.item()


def depth_error(x_pred, x_output):
    device = x_pred.device
    binary_mask = (torch.sum(x_output, dim=1) != 0).unsqueeze(1).to(device)
    x_pred_true = x_pred.masked_select(binary_mask)
    x_output_true = x_output.masked_select(binary_mask)
    abs_err = torch.abs(x_pred_true - x_output_true)
    rel_err = torch.abs(x_pred_true - x_output_true) / x_output_true
    return (torch.sum(abs_err) / torch.nonzero(binary_mask, as_tuple=False).size(0)).item(), \
           (torch.sum(rel_err) / torch.nonzero(binary_mask, as_tuple=False).size(0)).item()


def normal_error(x_pred, x_output):
    binary_mask = (torch.sum(x_output, dim=1) != 0)
    error = torch.acos(torch.clamp(torch.sum(x_pred * x_output, 1).masked_select(binary_mask), -1, 1)).detach().cpu().numpy()
    error = np.degrees(error)
    return np.mean(error), np.median(error), np.mean(error < 11.25), np.mean(error < 22.5), np.mean(error < 30)


"""
=========== Universal Multi-task Trainer =========== 
"""


def multi_task_trainer(train_loader, test_loader, multi_task_model, device, optimizer, scheduler, opt, total_epoch=200):
    train_batch = len(train_loader)
    test_batch = len(test_loader)
    T = opt.temp
    avg_cost = np.zeros([total_epoch, 24], dtype=np.float32)
    lambda_weight = np.ones([3, total_epoch])
    for index in range(total_epoch):
        cost = np.zeros(24, dtype=np.float32)

        # apply Dynamic Weight Average
        if opt.weight == 'dwa':
            if index == 0 or index == 1:
                lambda_weight[:, index] = 1.0
            else:
                w_1 = avg_cost[index - 1, 0] / avg_cost[index - 2, 0]
                w_2 = avg_cost[index - 1, 3] / avg_cost[index - 2, 3]
                w_3 = avg_cost[index - 1, 6] / avg_cost[index - 2, 6]
                lambda_weight[0, index] = 3 * np.exp(w_1 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))
                lambda_weight[1, index] = 3 * np.exp(w_2 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))
                lambda_weight[2, index] = 3 * np.exp(w_3 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))

        # iteration for all batches
        multi_task_model.train()
        train_dataset = iter(train_loader)
        conf_mat = ConfMatrix(multi_task_model.class_nb)
        for k in range(train_batch):
            train_data, train_label, train_depth, train_normal = train_dataset.next()
            train_data, train_label = train_data.to(device), train_label.long().to(device)
            train_depth, train_normal = train_depth.to(device), train_normal.to(device)

            train_pred, logsigma = multi_task_model(train_data)

            optimizer.zero_grad()
            train_loss = [model_fit(train_pred[0], train_label, 'semantic'),
                          model_fit(train_pred[1], train_depth, 'depth'),
                          model_fit(train_pred[2], train_normal, 'normal')]

            if opt.weight == 'equal' or opt.weight == 'dwa':
                loss = sum([lambda_weight[i, index] * train_loss[i] for i in range(3)])
            else:
                loss = sum(1 / (2 * torch.exp(logsigma[i])) * train_loss[i] + logsigma[i] / 2 for i in range(3))

            loss.backward()
            optimizer.step()

            # accumulate label prediction for every pixel in training images
            conf_mat.update(train_pred[0].argmax(1).flatten(), train_label.flatten())

            cost[0] = train_loss[0].item()
            cost[3] = train_loss[1].item()
            cost[4], cost[5] = depth_error(train_pred[1], train_depth)
            cost[6] = train_loss[2].item()
            cost[7], cost[8], cost[9], cost[10], cost[11] = normal_error(train_pred[2], train_normal)
            avg_cost[index, :12] += cost[:12] / train_batch

        # compute mIoU and acc
        avg_cost[index, 1:3] = np.array(conf_mat.get_metrics())

        # evaluating test data
        multi_task_model.eval()
        conf_mat = ConfMatrix(multi_task_model.class_nb)
        with torch.no_grad():  # operations inside don't track history
            test_dataset = iter(test_loader)
            for k in range(test_batch):
                test_data, test_label, test_depth, test_normal = test_dataset.next()
                test_data, test_label = test_data.to(device), test_label.long().to(device)
                test_depth, test_normal = test_depth.to(device), test_normal.to(device)

                test_pred, _ = multi_task_model(test_data)
                test_loss = [model_fit(test_pred[0], test_label, 'semantic'),
                             model_fit(test_pred[1], test_depth, 'depth'),
                             model_fit(test_pred[2], test_normal, 'normal')]

                conf_mat.update(test_pred[0].argmax(1).flatten(), test_label.flatten())

                cost[12] = test_loss[0].item()
                cost[15] = test_loss[1].item()
                cost[16], cost[17] = depth_error(test_pred[1], test_depth)
                cost[18] = test_loss[2].item()
                cost[19], cost[20], cost[21], cost[22], cost[23] = normal_error(test_pred[2], test_normal)
                avg_cost[index, 12:] += cost[12:] / test_batch

            # compute mIoU and acc
            avg_cost[index, 13:15] = np.array(conf_mat.get_metrics())

        scheduler.step()
        print('Epoch: {:04d} | TRAIN: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} ||'
            'TEST: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} '
            .format(index, avg_cost[index, 0], avg_cost[index, 1], avg_cost[index, 2], avg_cost[index, 3],
                    avg_cost[index, 4], avg_cost[index, 5], avg_cost[index, 6], avg_cost[index, 7], avg_cost[index, 8],
                    avg_cost[index, 9], avg_cost[index, 10], avg_cost[index, 11], avg_cost[index, 12], avg_cost[index, 13],
                    avg_cost[index, 14], avg_cost[index, 15], avg_cost[index, 16], avg_cost[index, 17], avg_cost[index, 18],
                    avg_cost[index, 19], avg_cost[index, 20], avg_cost[index, 21], avg_cost[index, 22], avg_cost[index, 23]))
